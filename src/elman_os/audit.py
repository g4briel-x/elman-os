"""Authenticated and privacy-preserving audit envelope for AI executions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
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
class AuditTrail:
    signer: AuditSigner
    sink: AuditSink
    event_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _previous_signature: str | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
