"""Authenticated and privacy-preserving audit envelope for AI executions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .execution import AIExecutionError, ExecutionResult, ResilientAIExecutor
from .provider import ModelRequest, ProviderError

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
EXECUTE_ROLE = "ai.execute"


class AuthenticationMethod(StrEnum):
    JWT = "jwt"
    OIDC = "oidc"
    API_KEY = "api_key"
    LOCAL_TEST = "local_test"
    ANONYMOUS = "anonymous"


class ExecutionPurpose(StrEnum):
    AGENT_TASK = "agent_task"
    EVALUATION = "evaluation"
    SYSTEM_OPERATION = "system_operation"


class AuditEventType(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class ExecutionAuthorizationError(PermissionError):
    """Safe denial raised before a provider can be contacted."""


class AuditIntegrityError(RuntimeError):
    """Audit signing, chaining or persistence failed."""


@dataclass(frozen=True, slots=True)
class ExecutionPrincipal:
    """Verified identity supplied by the authentication boundary."""

    subject_id: str
    tenant_id: str
    authentication_method: AuthenticationMethod
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.subject_id.strip() or not self.tenant_id.strip():
            raise ValueError("subject_id et tenant_id sont obligatoires")
        if any(not role.strip() or len(role) > 128 for role in self.roles):
            raise ValueError("Les rôles doivent être des identifiants non vides")


@dataclass(frozen=True, slots=True)
class ExecutionAuditContext:
    principal: ExecutionPrincipal
    purpose: ExecutionPurpose
    correlation_id: str

    def __post_init__(self) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(self.correlation_id):
            raise ValueError("correlation_id doit être un identifiant sûr")


@dataclass(frozen=True, slots=True)
class ExecutionAuthorizationPolicy:
    required_role: str = EXECUTE_ROLE
    allowed_methods: frozenset[AuthenticationMethod] = field(
        default_factory=lambda: frozenset(
            {
                AuthenticationMethod.JWT,
                AuthenticationMethod.OIDC,
                AuthenticationMethod.API_KEY,
                AuthenticationMethod.LOCAL_TEST,
            }
        )
    )
    allowed_purposes: frozenset[ExecutionPurpose] = field(
        default_factory=lambda: frozenset(ExecutionPurpose)
    )

    def authorize(self, context: ExecutionAuditContext) -> None:
        principal = context.principal
        if principal.authentication_method not in self.allowed_methods:
            raise ExecutionAuthorizationError(
                "La méthode d'authentification ne permet pas l'exécution IA"
            )
        if self.required_role not in principal.roles:
            raise ExecutionAuthorizationError(
                "Le principal ne possède pas le rôle d'exécution IA requis"
            )
        if context.purpose not in self.allowed_purposes:
            raise ExecutionAuthorizationError(
                "Le motif d'exécution IA n'est pas autorisé"
            )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Deliberately small event schema: no prompt, response, secret or metadata."""

    schema_version: int
    event_id: str
    occurred_at: str
    event_type: AuditEventType
    correlation_id: str
    principal_fingerprint: str
    tenant_fingerprint: str
    authentication_method: AuthenticationMethod
    purpose: ExecutionPurpose
    request_fingerprint: str
    provider_id: str
    model: str
    attempts: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: int
    error_code: str | None = None

    def canonical_bytes(self) -> bytes:
        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        payload["authentication_method"] = self.authentication_method.value
        payload["purpose"] = self.purpose.value
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def to_safe_dict(self) -> dict[str, object]:
        return json.loads(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class SignedAuditEvent:
    event: AuditEvent
    previous_signature: str | None
    signature: str

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "event": self.event.to_safe_dict(),
            "previous_signature": self.previous_signature,
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class AuditSigner:
    """HMAC signer whose key is never exposed by string representations."""

    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) < 32:
            raise ValueError("La clé de signature d'audit doit contenir au moins 32 octets")

    def __repr__(self) -> str:
        return "AuditSigner(key=<redacted>)"

    __str__ = __repr__

    def fingerprint(self, namespace: str, value: str) -> str:
        payload = f"{namespace}\0{value}".encode("utf-8")
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def sign(self, event: AuditEvent, previous_signature: str | None) -> str:
        chain = (previous_signature or "").encode("ascii")
        return hmac.new(
            self.key,
            chain + b"\0" + event.canonical_bytes(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, signed: SignedAuditEvent) -> bool:
        expected = self.sign(signed.event, signed.previous_signature)
        return hmac.compare_digest(expected, signed.signature)


@runtime_checkable
class AuditSink(Protocol):
    async def append(self, event: SignedAuditEvent) -> None:
        """Append one immutable event or fail."""


@dataclass(slots=True)
class InMemoryAuditSink:
    events: list[SignedAuditEvent] = field(default_factory=list)

    async def append(self, event: SignedAuditEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class FileAuditSink:
    """Append-only JSONL sink with bounded recovery and durable writes."""

    path: Path
    max_file_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes doit être positif")
        if self.path.exists() and self.path.is_symlink():
            raise AuditIntegrityError("Le journal d'audit ne peut pas être un lien")

    async def append(self, event: SignedAuditEvent) -> None:
        payload = (
            json.dumps(
                event.to_safe_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        await asyncio.to_thread(self._append_sync, payload)

    def _append_sync(self, payload: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() and self.path.is_symlink():
            raise AuditIntegrityError("Le journal d'audit ne peut pas être un lien")
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size + len(payload) > self.max_file_bytes:
            raise AuditIntegrityError("Le journal d'audit a atteint sa taille maximale")

        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written < 1:
                    raise AuditIntegrityError(
                        "L'écriture durable du journal d'audit a échoué"
                    )
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Windows ACLs are managed by the host; durability still applies.
            pass

    def load(self) -> tuple[SignedAuditEvent, ...]:
        if not self.path.exists():
            return ()
        if self.path.is_symlink():
            raise AuditIntegrityError("Le journal d'audit ne peut pas être un lien")
        if self.path.stat().st_size > self.max_file_bytes:
            raise AuditIntegrityError("Le journal d'audit dépasse la taille maximale")
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            return tuple(self._decode(line) for line in lines if line.strip())
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise AuditIntegrityError(
                "Le journal d'audit persistant est illisible"
            ) from exc

    @staticmethod
    def _decode(line: str) -> SignedAuditEvent:
        record = json.loads(line)
        payload = record["event"]
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
        return SignedAuditEvent(
            event=event,
            previous_signature=(
                str(record["previous_signature"])
                if record.get("previous_signature") is not None
                else None
            ),
            signature=str(record["signature"]),
        )


@dataclass(slots=True)
class AuditTrail:
    signer: AuditSigner
    sink: AuditSink
    event_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _previous_signature: str | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @classmethod
    def resume(
        cls,
        signer: AuditSigner,
        sink: FileAuditSink,
        **kwargs: object,
    ) -> "AuditTrail":
        trail = cls(signer=signer, sink=sink, **kwargs)
        events = sink.load()
        if not trail.verify_chain(events):
            raise AuditIntegrityError("La chaîne d'audit persistante est invalide")
        if events:
            trail._previous_signature = events[-1].signature
        return trail

    async def append(self, event: AuditEvent) -> SignedAuditEvent:
        async with self._lock:
            signed = SignedAuditEvent(
                event=event,
                previous_signature=self._previous_signature,
                signature=self.signer.sign(event, self._previous_signature),
            )
            try:
                await self.sink.append(signed)
            except Exception as exc:
                raise AuditIntegrityError(
                    "L'événement d'audit n'a pas pu être enregistré"
                ) from exc
            self._previous_signature = signed.signature
            return signed

    def verify_chain(self, events: Sequence[SignedAuditEvent]) -> bool:
        previous: str | None = None
        for signed in events:
            if signed.previous_signature != previous or not self.signer.verify(signed):
                return False
            previous = signed.signature
        return True


@dataclass(slots=True)
class AuditedAIExecutor:
    """Authorize, execute and produce a signed minimal audit trail."""

    executor: ResilientAIExecutor
    trail: AuditTrail
    policy: ExecutionAuthorizationPolicy = field(
        default_factory=ExecutionAuthorizationPolicy
    )
    clock: Callable[[], float] = time.monotonic

    async def record_denial(
        self,
        request: ModelRequest,
        context: ExecutionAuditContext,
        *,
        error_code: str,
        started_at: float | None = None,
    ) -> SignedAuditEvent:
        """Record a policy denial without exposing request payloads."""

        return await self._record(
            AuditEventType.DENIED,
            request,
            context,
            self.clock() if started_at is None else started_at,
            error_code=error_code,
        )

    async def generate(
        self,
        request: ModelRequest,
        context: ExecutionAuditContext,
    ) -> ExecutionResult:
        started_at = self.clock()
        try:
            self.policy.authorize(context)
        except ExecutionAuthorizationError:
            await self._record(
                AuditEventType.DENIED,
                request,
                context,
                started_at,
                error_code="authorization_denied",
            )
            raise

        await self._record(AuditEventType.STARTED, request, context, started_at)
        try:
            result = await self.executor.generate(request)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._record(
                    AuditEventType.CANCELLED,
                    request,
                    context,
                    started_at,
                    error_code="cancelled",
                )
            )
            raise
        except (AIExecutionError, ProviderError) as exc:
            code = getattr(exc, "code", "unknown")
            await self._record(
                AuditEventType.FAILED,
                request,
                context,
                started_at,
                error_code=getattr(code, "value", str(code)),
            )
            raise

        await self._record(
            AuditEventType.SUCCEEDED,
            request,
            context,
            started_at,
            result=result,
        )
        return result

    async def _record(
        self,
        event_type: AuditEventType,
        request: ModelRequest,
        context: ExecutionAuditContext,
        started_at: float,
        *,
        result: ExecutionResult | None = None,
        error_code: str | None = None,
    ) -> SignedAuditEvent:
        signer = self.trail.signer
        snapshot = self.executor.ledger.snapshot()
        event = AuditEvent(
            schema_version=1,
            event_id=self.trail.event_id_factory(),
            occurred_at=self.trail.wall_clock().isoformat(),
            event_type=event_type,
            correlation_id=context.correlation_id,
            principal_fingerprint=signer.fingerprint(
                "principal", context.principal.subject_id
            ),
            tenant_fingerprint=signer.fingerprint(
                "tenant", context.principal.tenant_id
            ),
            authentication_method=context.principal.authentication_method,
            purpose=context.purpose,
            request_fingerprint=signer.fingerprint(
                "request", request.request_id
            ),
            provider_id=self._safe_identifier(
                "provider", self.executor.provider.descriptor.provider_id
            ),
            model=self._safe_identifier("model", request.model),
            attempts=(
                result.attempts if result is not None else snapshot.provider_calls
            ),
            input_tokens=(
                result.response.usage.input_tokens if result is not None else 0
            ),
            output_tokens=(
                result.response.usage.output_tokens if result is not None else 0
            ),
            elapsed_ms=max(0, int((self.clock() - started_at) * 1000)),
            error_code=error_code,
        )
        return await self.trail.append(event)

    def _safe_identifier(self, namespace: str, value: str) -> str:
        if _SAFE_IDENTIFIER.fullmatch(value):
            return value
        return f"redacted-{self.trail.signer.fingerprint(namespace, value)[:24]}"
