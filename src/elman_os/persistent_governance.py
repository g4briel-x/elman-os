"""Persistent multi-instance quotas and signed audit chains for ELMAN-OS."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import (
    AuditEvent,
    AuditEventType,
    AuditIntegrityError,
    AuditSigner,
    AuthenticationMethod,
    ExecutionPurpose,
    SignedAuditEvent,
)
from .governance import (
    IdentityQuota,
    IdentityQuotaErrorCode,
    IdentityQuotaExceededError,
    IdentityUsage,
    QuotaReservation,
)
from .transactional_persistence import (
    PersistenceBackend,
    PersistenceError,
    StoredRecord,
)

_QUOTA_NAMESPACE = "identity-quotas-v1"
_AUDIT_NAMESPACE = "audit-v1"
_STATE_KEY = "state"
_MAX_AUDIT_EVENTS = 10_000_000


def _positive_number(name: str, value: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} doit être un nombre")
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} doit être compris entre 0 et {maximum:g}")
    return float(value)


def _usage_key(identity_fingerprint: str) -> str:
    if (
        not isinstance(identity_fingerprint, str)
        or len(identity_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in identity_fingerprint)
    ):
        raise ValueError("identity_fingerprint doit être un SHA-256 hexadécimal")
    return f"usage-{identity_fingerprint}"


def _reservation_key(identity_fingerprint: str, reservation_id: str) -> str:
    _usage_key(identity_fingerprint)
    try:
        normalized = str(uuid.UUID(reservation_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("reservation_id doit être un UUID valide") from exc
    return f"reservation-{identity_fingerprint}-{normalized}"


@dataclass(slots=True)
class PersistentIdentityQuotaManager:
    """Drop-in persistent counterpart of ``IdentityQuotaManager``.

    One instance is bound to one tenant. Atomic transactions make request,
    token and concurrency limits consistent across processes and hosts sharing
    the same persistence backend.
    """

    tenant_id: str
    backend: PersistenceBackend
    quota: IdentityQuota = field(default_factory=IdentityQuota)
    reservation_ttl_seconds: float = 300.0
    wall_clock: Callable[[], float] = time.time
    namespace: str = _QUOTA_NAMESPACE

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id est obligatoire")
        self.reservation_ttl_seconds = _positive_number(
            "reservation_ttl_seconds",
            self.reservation_ttl_seconds,
            86_400.0,
        )

    async def reserve(
        self,
        identity_fingerprint: str,
        estimated_tokens: int,
    ) -> QuotaReservation:
        usage_key = _usage_key(identity_fingerprint)
        if (
            not isinstance(estimated_tokens, int)
            or isinstance(estimated_tokens, bool)
            or estimated_tokens < 1
        ):
            raise ValueError("estimated_tokens doit être un entier positif")
        now = float(self.wall_clock())
        reservation = QuotaReservation(
            reservation_id=str(uuid.uuid4()),
            identity_fingerprint=identity_fingerprint,
            reserved_tokens=estimated_tokens,
        )

        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                usage_record = await transaction.get(usage_key)
                usage = self._decode_usage(usage_record)
                usage = await self._reap_expired(
                    transaction,
                    identity_fingerprint,
                    usage,
                    now,
                )
                if usage["requests"] >= self.quota.max_requests:
                    raise IdentityQuotaExceededError(
                        IdentityQuotaErrorCode.REQUEST_LIMIT
                    )
                if usage["active"] >= self.quota.max_concurrent:
                    raise IdentityQuotaExceededError(
                        IdentityQuotaErrorCode.CONCURRENCY_LIMIT
                    )
                projected = (
                    usage["tokens"] + usage["reserved_tokens"] + estimated_tokens
                )
                if projected > self.quota.max_tokens:
                    raise IdentityQuotaExceededError(
                        IdentityQuotaErrorCode.TOKEN_LIMIT
                    )

                usage["requests"] += 1
                usage["active"] += 1
                usage["reserved_tokens"] += estimated_tokens
                await transaction.put(
                    usage_key,
                    usage,
                    expected_version=(
                        usage_record.version if usage_record is not None else 0
                    ),
                )
                await transaction.put(
                    _reservation_key(
                        identity_fingerprint, reservation.reservation_id
                    ),
                    {
                        "identity_fingerprint": identity_fingerprint,
                        "reserved_tokens": estimated_tokens,
                        "expires_at": now + self.reservation_ttl_seconds,
                    },
                    expected_version=0,
                )
        except IdentityQuotaExceededError:
            raise
        except (PersistenceError, ValueError, TypeError) as exc:
            raise RuntimeError(
                "La réservation persistante de quota a échoué"
            ) from exc
        return reservation

    async def settle(
        self,
        reservation: QuotaReservation,
        *,
        actual_tokens: int,
    ) -> None:
        if (
            not isinstance(actual_tokens, int)
            or isinstance(actual_tokens, bool)
            or actual_tokens < 0
        ):
            raise ValueError("actual_tokens doit être un entier positif ou nul")
        reservation_key = _reservation_key(
            reservation.identity_fingerprint,
            reservation.reservation_id,
        )
        usage_key = _usage_key(reservation.identity_fingerprint)
        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                stored_reservation = await transaction.get(reservation_key)
                if stored_reservation is None:
                    raise ValueError(
                        "La réservation de quota est inconnue ou déjà réglée"
                    )
                payload = self._decode_reservation(stored_reservation)
                if (
                    payload["identity_fingerprint"]
                    != reservation.identity_fingerprint
                    or payload["reserved_tokens"] != reservation.reserved_tokens
                ):
                    raise ValueError("La réservation de quota ne correspond pas")

                usage_record = await transaction.get(usage_key)
                if usage_record is None:
                    raise RuntimeError("L'état de quota persistant est absent")
                usage = self._decode_usage(usage_record)
                usage["active"] = max(0, usage["active"] - 1)
                usage["reserved_tokens"] = max(
                    0,
                    usage["reserved_tokens"] - reservation.reserved_tokens,
                )
                usage["tokens"] += actual_tokens
                await transaction.put(
                    usage_key,
                    usage,
                    expected_version=usage_record.version,
                )
                await transaction.delete(
                    reservation_key,
                    expected_version=stored_reservation.version,
                )
        except ValueError:
            raise
        except (PersistenceError, TypeError) as exc:
            raise RuntimeError("Le règlement persistant du quota a échoué") from exc

    async def snapshot(self, identity_fingerprint: str) -> IdentityUsage:
        usage_key = _usage_key(identity_fingerprint)
        now = float(self.wall_clock())
        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                usage_record = await transaction.get(usage_key)
                usage = self._decode_usage(usage_record)
                usage = await self._reap_expired(
                    transaction,
                    identity_fingerprint,
                    usage,
                    now,
                )
                if usage_record is not None:
                    current = await transaction.get(usage_key)
                    if current is not None and dict(current.value) != usage:
                        await transaction.put(
                            usage_key,
                            usage,
                            expected_version=current.version,
                        )
                return IdentityUsage(**usage)
        except (PersistenceError, TypeError, ValueError) as exc:
            raise RuntimeError("La lecture persistante du quota a échoué") from exc

    async def _reap_expired(
        self,
        transaction: Any,
        identity_fingerprint: str,
        usage: dict[str, int],
        now: float,
    ) -> dict[str, int]:
        prefix = f"reservation-{identity_fingerprint}-"
        offset = 0
        expired: list[StoredRecord] = []
        while True:
            batch = await transaction.list(prefix=prefix, limit=1000, offset=offset)
            if not batch:
                break
            for record in batch:
                payload = self._decode_reservation(record)
                if payload["expires_at"] <= now:
                    expired.append(record)
            if len(batch) < 1000:
                break
            offset += len(batch)

        for record in expired:
            payload = self._decode_reservation(record)
            usage["active"] = max(0, usage["active"] - 1)
            usage["reserved_tokens"] = max(
                0, usage["reserved_tokens"] - payload["reserved_tokens"]
            )
            await transaction.delete(record.key, expected_version=record.version)
        return usage

    @staticmethod
    def _decode_usage(record: StoredRecord | None) -> dict[str, int]:
        if record is None:
            return {
                "requests": 0,
                "tokens": 0,
                "active": 0,
                "reserved_tokens": 0,
            }
        try:
            usage = {
                name: int(record.value[name])
                for name in ("requests", "tokens", "active", "reserved_tokens")
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("L'état de quota persistant est invalide") from exc
        if any(value < 0 for value in usage.values()):
            raise RuntimeError("L'état de quota persistant est invalide")
        return usage

    @staticmethod
    def _decode_reservation(record: StoredRecord) -> dict[str, Any]:
        try:
            fingerprint = str(record.value["identity_fingerprint"])
            reserved = int(record.value["reserved_tokens"])
            expires_at = float(record.value["expires_at"])
            _usage_key(fingerprint)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Une réservation persistante est invalide") from exc
        if reserved < 1:
            raise RuntimeError("Une réservation persistante est invalide")
        return {
            "identity_fingerprint": fingerprint,
            "reserved_tokens": reserved,
            "expires_at": expires_at,
        }


@dataclass(slots=True)
class PersistentAuditTrail:
    """Tenant-bound, atomic signed audit chain shared by multiple instances."""

    signer: AuditSigner
    backend: PersistenceBackend
    tenant_id: str
    namespace: str = _AUDIT_NAMESPACE
    event_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id est obligatoire")

    async def append(self, event: AuditEvent) -> SignedAuditEvent:
        expected_tenant = self.signer.fingerprint("tenant", self.tenant_id)
        if event.tenant_fingerprint != expected_tenant:
            raise AuditIntegrityError(
                "L'événement d'audit ne correspond pas au tenant du journal"
            )
        signer_id = self.signer.fingerprint("audit-signer", "v1")
        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                state_record = await transaction.get(_STATE_KEY)
                state = self._decode_state(state_record)
                if state["signer_id"] not in (None, signer_id):
                    raise AuditIntegrityError(
                        "La clé de signature ne correspond pas au journal"
                    )
                sequence = state["sequence"] + 1
                if sequence > _MAX_AUDIT_EVENTS:
                    raise AuditIntegrityError(
                        "Le journal d'audit a atteint sa capacité maximale"
                    )
                signed = SignedAuditEvent(
                    event=event,
                    previous_signature=state["signature"],
                    signature=self.signer.sign(event, state["signature"]),
                )
                await transaction.put(
                    self._event_key(sequence),
                    signed.to_safe_dict(),
                    expected_version=0,
                )
                await transaction.put(
                    _STATE_KEY,
                    {
                        "schema_version": 1,
                        "sequence": sequence,
                        "signature": signed.signature,
                        "signer_id": signer_id,
                    },
                    expected_version=(
                        state_record.version if state_record is not None else 0
                    ),
                )
                return signed
        except AuditIntegrityError:
            raise
        except (PersistenceError, TypeError, ValueError) as exc:
            raise AuditIntegrityError(
                "L'événement d'audit persistant n'a pas pu être enregistré"
            ) from exc

    async def load(self) -> tuple[SignedAuditEvent, ...]:
        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                return tuple(await self._load_events(transaction))
        except AuditIntegrityError:
            raise
        except (PersistenceError, TypeError, ValueError) as exc:
            raise AuditIntegrityError(
                "Le journal d'audit persistant est illisible"
            ) from exc

    async def verify_persisted(self) -> bool:
        try:
            async with self.backend.transaction(
                self.tenant_id, self.namespace
            ) as transaction:
                state = self._decode_state(await transaction.get(_STATE_KEY))
                events = await self._load_events(transaction)
                if state["sequence"] != len(events):
                    return False
                expected_signature = events[-1].signature if events else None
                return (
                    state["signature"] == expected_signature
                    and self.verify_chain(events)
                )
        except (AuditIntegrityError, PersistenceError, TypeError, ValueError):
            return False

    def verify_chain(self, events: Sequence[SignedAuditEvent]) -> bool:
        previous: str | None = None
        for signed in events:
            if (
                signed.previous_signature != previous
                or not self.signer.verify(signed)
            ):
                return False
            previous = signed.signature
        return True

    async def _load_events(self, transaction: Any) -> list[SignedAuditEvent]:
        events: list[SignedAuditEvent] = []
        offset = 0
        while True:
            batch = await transaction.list(
                prefix="event-", limit=1000, offset=offset
            )
            if not batch:
                break
            events.extend(self._decode_signed(record.value) for record in batch)
            if len(batch) < 1000:
                break
            offset += len(batch)
        return events

    @staticmethod
    def _event_key(sequence: int) -> str:
        return f"event-{sequence:020d}"

    @staticmethod
    def _decode_state(record: StoredRecord | None) -> dict[str, Any]:
        if record is None:
            return {"sequence": 0, "signature": None, "signer_id": None}
        try:
            if int(record.value["schema_version"]) != 1:
                raise ValueError
            sequence = int(record.value["sequence"])
            signature = record.value["signature"]
            signer_id = record.value["signer_id"]
            if sequence < 1 or not isinstance(signature, str):
                raise ValueError
            if not isinstance(signer_id, str):
                raise ValueError
            return {
                "sequence": sequence,
                "signature": signature,
                "signer_id": signer_id,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditIntegrityError("L'état du journal d'audit est invalide") from exc

    @staticmethod
    def _decode_signed(value: Mapping[str, Any]) -> SignedAuditEvent:
        try:
            record = dict(value)
            payload = dict(record["event"])
            event = AuditEvent(
                schema_version=int(payload["schema_version"]),
                event_id=str(payload["event_id"]),
                occurred_at=str(payload["occurred_at"]),
                event_type=AuditEventType(payload["event_type"]),
                correlation_id=str(payload["correlation_id"]),
                principal_fingerprint=str(payload["principal_fingerprint"]),
                tenant_fingerprint=str(payload["tenant_fingerprint"]),
                authentication_method=AuthenticationMethod(
                    payload["authentication_method"]
                ),
                purpose=ExecutionPurpose(payload["purpose"]),
                request_fingerprint=str(payload["request_fingerprint"]),
                provider_id=str(payload["provider_id"]),
                model=str(payload["model"]),
                attempts=int(payload["attempts"]),
                input_tokens=int(payload["input_tokens"]),
                output_tokens=int(payload["output_tokens"]),
                elapsed_ms=int(payload["elapsed_ms"]),
                error_code=(
                    str(payload["error_code"])
                    if payload.get("error_code") is not None
                    else None
                ),
            )
            previous = record.get("previous_signature")
            signature = record["signature"]
            if previous is not None and not isinstance(previous, str):
                raise TypeError
            if not isinstance(signature, str):
                raise TypeError
            return SignedAuditEvent(event, previous, signature)
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise AuditIntegrityError(
                "Un événement d'audit persistant est invalide"
            ) from exc
