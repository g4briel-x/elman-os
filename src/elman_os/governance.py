"""Stabilized AI runtime: compatibility checks and per-identity quotas."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .audit import (
    AuditSigner,
    AuditSink,
    AuditTrail,
    AuditedAIExecutor,
    ExecutionAuditContext,
    ExecutionAuthorizationError,
    FileAuditSink,
)
from .configuration import ProviderSettings
from .execution import ExecutionResult
from .provider import ModelCapability, ModelRequest
from .registry import (
    DETERMINISTIC_PROVIDER_ID,
    ConfiguredAIRuntime,
    ProviderRegistry,
)


class CompatibilityErrorCode(StrEnum):
    PROVIDER_NOT_REGISTERED = "provider_not_registered"
    MODEL_NOT_DECLARED = "model_not_declared"
    CAPABILITY_NOT_DECLARED = "capability_not_declared"
    AUTH_MODE_INCOMPATIBLE = "auth_mode_incompatible"
    BASE_URL_REQUIRED = "base_url_required"
    DETERMINISTIC_REMOTE_SETTINGS = "deterministic_remote_settings"


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    valid: bool
    provider_id: str
    model: str
    error_codes: tuple[CompatibilityErrorCode, ...]

    def safe_summary(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "provider_id": self.provider_id,
            "model": self.model,
            "error_codes": [item.value for item in self.error_codes],
        }


class ConfigurationCompatibilityError(ValueError):
    def __init__(self, report: CompatibilityReport) -> None:
        super().__init__("La configuration IA est incompatible avec le registre")
        self.report = report


def check_configuration_compatibility(
    settings: ProviderSettings,
    registry: ProviderRegistry,
    *,
    required_capabilities: frozenset[ModelCapability] = frozenset(
        {ModelCapability.TEXT_GENERATION}
    ),
) -> CompatibilityReport:
    """Validate configuration against descriptors without creating a provider."""

    errors: list[CompatibilityErrorCode] = []
    registration = registry.registrations.get(settings.provider_id)
    if registration is None:
        errors.append(CompatibilityErrorCode.PROVIDER_NOT_REGISTERED)
    else:
        descriptor = registration.descriptor
        if descriptor.models and settings.model not in descriptor.models:
            errors.append(CompatibilityErrorCode.MODEL_NOT_DECLARED)
        if not required_capabilities.issubset(descriptor.capabilities):
            errors.append(CompatibilityErrorCode.CAPABILITY_NOT_DECLARED)

    if settings.provider_id == DETERMINISTIC_PROVIDER_ID:
        if (
            settings.auth_mode != "none"
            or settings.api_key is not None
            or settings.base_url is not None
        ):
            errors.append(CompatibilityErrorCode.DETERMINISTIC_REMOTE_SETTINGS)
    else:
        if settings.auth_mode != "api_key" or settings.api_key is None:
            errors.append(CompatibilityErrorCode.AUTH_MODE_INCOMPATIBLE)
        if settings.provider_id == "openai-compatible" and settings.base_url is None:
            errors.append(CompatibilityErrorCode.BASE_URL_REQUIRED)

    return CompatibilityReport(
        valid=not errors,
        provider_id=settings.provider_id,
        model=settings.model,
        error_codes=tuple(errors),
    )


class IdentityQuotaErrorCode(StrEnum):
    REQUEST_LIMIT = "identity_request_limit"
    TOKEN_LIMIT = "identity_token_limit"
    CONCURRENCY_LIMIT = "identity_concurrency_limit"


class IdentityQuotaExceededError(RuntimeError):
    def __init__(self, code: IdentityQuotaErrorCode) -> None:
        super().__init__("Le quota d'exécution IA de l'identité est dépassé")
        self.code = code


@dataclass(frozen=True, slots=True)
class IdentityQuota:
    max_requests: int = 100
    max_tokens: int = 1_000_000
    max_concurrent: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.max_requests <= 1_000_000:
            raise ValueError("max_requests doit être compris entre 1 et 1 000 000")
        if not 1 <= self.max_tokens <= 1_000_000_000:
            raise ValueError("max_tokens doit être compris entre 1 et 1 000 000 000")
        if not 1 <= self.max_concurrent <= 1_000:
            raise ValueError("max_concurrent doit être compris entre 1 et 1 000")


@dataclass(frozen=True, slots=True)
class IdentityUsage:
    requests: int
    tokens: int
    active: int
    reserved_tokens: int


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    identity_fingerprint: str
    reserved_tokens: int


@dataclass(slots=True)
class _MutableIdentityUsage:
    requests: int = 0
    tokens: int = 0
    active: int = 0
    reserved_tokens: int = 0


@dataclass(slots=True)
class IdentityQuotaManager:
    """Atomic, process-local quota accounting keyed only by HMAC fingerprints."""

    quota: IdentityQuota = field(default_factory=IdentityQuota)
    _usage: dict[str, _MutableIdentityUsage] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def reserve(
        self,
        identity_fingerprint: str,
        estimated_tokens: int,
    ) -> QuotaReservation:
        if not identity_fingerprint or estimated_tokens < 1:
            raise ValueError("Une identité et une estimation positive sont requises")
        async with self._lock:
            usage = self._usage.setdefault(
                identity_fingerprint, _MutableIdentityUsage()
            )
            if usage.requests >= self.quota.max_requests:
                raise IdentityQuotaExceededError(
                    IdentityQuotaErrorCode.REQUEST_LIMIT
                )
            if usage.active >= self.quota.max_concurrent:
                raise IdentityQuotaExceededError(
                    IdentityQuotaErrorCode.CONCURRENCY_LIMIT
                )
            projected = usage.tokens + usage.reserved_tokens + estimated_tokens
            if projected > self.quota.max_tokens:
                raise IdentityQuotaExceededError(IdentityQuotaErrorCode.TOKEN_LIMIT)
            usage.requests += 1
            usage.active += 1
            usage.reserved_tokens += estimated_tokens
            return QuotaReservation(identity_fingerprint, estimated_tokens)

    async def settle(
        self,
        reservation: QuotaReservation,
        *,
        actual_tokens: int,
    ) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens ne peut pas être négatif")
        async with self._lock:
            usage = self._usage[reservation.identity_fingerprint]
            usage.active = max(0, usage.active - 1)
            usage.reserved_tokens = max(
                0, usage.reserved_tokens - reservation.reserved_tokens
            )
            usage.tokens += actual_tokens

    async def snapshot(self, identity_fingerprint: str) -> IdentityUsage:
        async with self._lock:
            usage = self._usage.get(
                identity_fingerprint, _MutableIdentityUsage()
            )
            return IdentityUsage(
                requests=usage.requests,
                tokens=usage.tokens,
                active=usage.active,
                reserved_tokens=usage.reserved_tokens,
            )


@dataclass(slots=True)
class StabilizedAIExecutor:
    """Route one identity through authorization, quota, audit and execution."""

    audited: AuditedAIExecutor
    quotas: IdentityQuotaManager
    selected_model: str
    clock: Callable[[], float] = time.monotonic

    async def generate(
        self,
        request: ModelRequest,
        context: ExecutionAuditContext,
    ) -> ExecutionResult:
        routed = replace(request, model=self.selected_model)
        try:
            self.audited.policy.authorize(context)
        except ExecutionAuthorizationError:
            # The audited boundary owns normalized authorization and its event.
            return await self.audited.generate(routed, context)

        identity = self.audited.trail.signer.fingerprint(
            "quota", context.principal.subject_id
        )
        estimate = routed.max_output_tokens + sum(
            len(message.content.encode("utf-8")) for message in routed.messages
        )
        started_at = self.clock()
        try:
            reservation = await self.quotas.reserve(identity, estimate)
        except IdentityQuotaExceededError as exc:
            await self.audited.record_denial(
                routed,
                context,
                error_code=exc.code.value,
                started_at=started_at,
            )
            raise

        actual_tokens = 0
        try:
            result = await self.audited.generate(routed, context)
            actual_tokens = result.response.usage.total_tokens
            return result
        finally:
            await asyncio.shield(
                self.quotas.settle(
                    reservation,
                    actual_tokens=actual_tokens,
                )
            )


@dataclass(slots=True)
class StabilizedAIRuntime:
    """Fully composed alpha.7 runtime with an explicit close boundary."""

    configured: ConfiguredAIRuntime
    executor: StabilizedAIExecutor
    compatibility: CompatibilityReport

    @classmethod
    def from_settings(
        cls,
        registry: ProviderRegistry,
        settings: ProviderSettings,
        *,
        signer: AuditSigner,
        sink: AuditSink,
        quotas: IdentityQuotaManager | None = None,
        required_capabilities: frozenset[ModelCapability] = frozenset(
            {ModelCapability.TEXT_GENERATION}
        ),
    ) -> "StabilizedAIRuntime":
        report = check_configuration_compatibility(
            settings,
            registry,
            required_capabilities=required_capabilities,
        )
        if not report.valid:
            raise ConfigurationCompatibilityError(report)
        configured = ConfiguredAIRuntime.from_settings(
            registry,
            settings,
            required_capabilities=required_capabilities,
        )
        trail = (
            AuditTrail.resume(signer, sink)
            if isinstance(sink, FileAuditSink)
            else AuditTrail(signer, sink)
        )
        audited = AuditedAIExecutor(configured.executor, trail)
        return cls(
            configured=configured,
            executor=StabilizedAIExecutor(
                audited,
                quotas or IdentityQuotaManager(),
                configured.selection.selected_model,
            ),
            compatibility=report,
        )

    async def close(self) -> None:
        await self.configured.close()
