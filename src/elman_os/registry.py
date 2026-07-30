"""Explicit provider registration and configuration-driven selection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Mapping

from .configuration import ProviderSettings
from .execution import ExecutionResult, ResilientAIExecutor
from .provider import (
    AIProvider,
    DeterministicModelProvider,
    ModelCapability,
    ModelRequest,
    ProviderDescriptor,
)
from .openai_compatible import (
    AsyncHTTPTransport,
    OPENAI_COMPATIBLE_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    openai_compatible_descriptor,
    provider_from_settings,
)


DETERMINISTIC_PROVIDER_ID = "deterministic-model"
DETERMINISTIC_MODEL_ID = "deterministic-v1"


class RegistryErrorCode(StrEnum):
    DUPLICATE_PROVIDER = "duplicate_provider"
    PROVIDER_NOT_FOUND = "provider_not_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MODEL_NOT_SUPPORTED = "model_not_supported"
    CAPABILITY_NOT_SUPPORTED = "capability_not_supported"
    DESCRIPTOR_MISMATCH = "descriptor_mismatch"
    INVALID_PROVIDER = "invalid_provider"
    FALLBACK_NOT_AVAILABLE = "fallback_not_available"


class ProviderRegistryError(RuntimeError):
    """Portable selection failure without credentials or raw configuration."""

    def __init__(
        self,
        code: RegistryErrorCode,
        message: str,
        *,
        provider_id: str,
    ) -> None:
        if not message.strip():
            raise ValueError("Le message d'erreur ne peut pas être vide")
        if not provider_id.strip():
            raise ValueError("provider_id ne peut pas être vide")
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id


class ProviderUnavailableError(RuntimeError):
    """Factory signal allowing an explicitly configured safe fallback."""


ProviderFactory = Callable[[ProviderSettings], AIProvider]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    descriptor: ProviderDescriptor
    factory: ProviderFactory = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(self.factory):
            raise TypeError("factory doit être appelable")


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: AIProvider = field(repr=False)
    requested_provider_id: str
    selected_provider_id: str
    requested_model: str
    selected_model: str
    used_fallback: bool
    fallback_reason: RegistryErrorCode | None = None

    def safe_summary(self) -> dict[str, object]:
        return {
            "requested_provider_id": self.requested_provider_id,
            "selected_provider_id": self.selected_provider_id,
            "requested_model": self.requested_model,
            "selected_model": self.selected_model,
            "used_fallback": self.used_fallback,
            "fallback_reason": (
                self.fallback_reason.value
                if self.fallback_reason is not None
                else None
            ),
        }


@dataclass(slots=True)
class ProviderRegistry:
    """In-process registry with fail-closed model and capability checks."""

    _registrations: dict[str, ProviderRegistration] = field(default_factory=dict)

    @property
    def registrations(self) -> Mapping[str, ProviderRegistration]:
        return MappingProxyType(self._registrations)

    def register(self, registration: ProviderRegistration) -> None:
        provider_id = registration.descriptor.provider_id
        if provider_id in self._registrations:
            raise ProviderRegistryError(
                RegistryErrorCode.DUPLICATE_PROVIDER,
                f"Le fournisseur '{provider_id}' est déjà enregistré",
                provider_id=provider_id,
            )
        self._registrations[provider_id] = registration

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(
            registration.descriptor
            for _, registration in sorted(self._registrations.items())
        )

    def resolve(
        self,
        settings: ProviderSettings,
        *,
        required_capabilities: frozenset[ModelCapability] = frozenset(
            {ModelCapability.TEXT_GENERATION}
        ),
        allow_deterministic_fallback: bool = False,
    ) -> ProviderSelection:
        registration = self._registrations.get(settings.provider_id)
        fallback_reason: RegistryErrorCode | None = None

        if registration is None:
            fallback_reason = RegistryErrorCode.PROVIDER_NOT_FOUND
        else:
            self._validate_requirements(
                registration.descriptor,
                settings.model,
                required_capabilities,
            )
            try:
                provider = registration.factory(settings)
            except ProviderUnavailableError:
                fallback_reason = RegistryErrorCode.PROVIDER_UNAVAILABLE
            else:
                self._validate_instance(registration.descriptor, provider)
                return ProviderSelection(
                    provider=provider,
                    requested_provider_id=settings.provider_id,
                    selected_provider_id=settings.provider_id,
                    requested_model=settings.model,
                    selected_model=settings.model,
                    used_fallback=False,
                )

        if not allow_deterministic_fallback:
            code = fallback_reason or RegistryErrorCode.PROVIDER_UNAVAILABLE
            raise ProviderRegistryError(
                code,
                f"Le fournisseur '{settings.provider_id}' n'est pas disponible",
                provider_id=settings.provider_id,
            )
        return self._resolve_deterministic_fallback(
            settings,
            required_capabilities,
            fallback_reason or RegistryErrorCode.PROVIDER_UNAVAILABLE,
        )

    def _resolve_deterministic_fallback(
        self,
        settings: ProviderSettings,
        required_capabilities: frozenset[ModelCapability],
        reason: RegistryErrorCode,
    ) -> ProviderSelection:
        registration = self._registrations.get(DETERMINISTIC_PROVIDER_ID)
        if registration is None:
            raise ProviderRegistryError(
                RegistryErrorCode.FALLBACK_NOT_AVAILABLE,
                "Le fallback déterministe n'est pas enregistré",
                provider_id=DETERMINISTIC_PROVIDER_ID,
            )
        self._validate_requirements(
            registration.descriptor,
            DETERMINISTIC_MODEL_ID,
            required_capabilities,
        )
        fallback_settings = replace(
            settings,
            provider_id=DETERMINISTIC_PROVIDER_ID,
            model=DETERMINISTIC_MODEL_ID,
            auth_mode="none",
            api_key=None,
            base_url=None,
        )
        provider = registration.factory(fallback_settings)
        self._validate_instance(registration.descriptor, provider)
        return ProviderSelection(
            provider=provider,
            requested_provider_id=settings.provider_id,
            selected_provider_id=DETERMINISTIC_PROVIDER_ID,
            requested_model=settings.model,
            selected_model=DETERMINISTIC_MODEL_ID,
            used_fallback=True,
            fallback_reason=reason,
        )

    @staticmethod
    def _validate_requirements(
        descriptor: ProviderDescriptor,
        model: str,
        required_capabilities: frozenset[ModelCapability],
    ) -> None:
        if descriptor.models and model not in descriptor.models:
            raise ProviderRegistryError(
                RegistryErrorCode.MODEL_NOT_SUPPORTED,
                f"Le modèle '{model}' n'est pas déclaré par le fournisseur",
                provider_id=descriptor.provider_id,
            )
        missing = required_capabilities - descriptor.capabilities
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ProviderRegistryError(
                RegistryErrorCode.CAPABILITY_NOT_SUPPORTED,
                f"Capacités non prises en charge : {names}",
                provider_id=descriptor.provider_id,
            )

    @staticmethod
    def _validate_instance(
        expected: ProviderDescriptor,
        provider: AIProvider,
    ) -> None:
        if not isinstance(provider, AIProvider):
            raise ProviderRegistryError(
                RegistryErrorCode.INVALID_PROVIDER,
                "La factory n'a pas produit un fournisseur conforme",
                provider_id=expected.provider_id,
            )
        if provider.descriptor != expected:
            raise ProviderRegistryError(
                RegistryErrorCode.DESCRIPTOR_MISMATCH,
                "Le descriptor runtime diffère de l'enregistrement",
                provider_id=expected.provider_id,
            )


def built_in_provider_registry(
    *,
    transport_factories: Mapping[str, Callable[[], AsyncHTTPTransport]] | None = None,
) -> ProviderRegistry:
    """Return built-ins without contacting any provider.

    ``transport_factories`` is primarily an offline-test seam. A default
    standard-library transport is constructed lazily by each real adapter.
    """

    registry = ProviderRegistry()
    descriptor = DeterministicModelProvider().descriptor
    registry.register(
        ProviderRegistration(
            descriptor=descriptor,
            factory=lambda settings: DeterministicModelProvider(),
        )
    )
    injected = transport_factories or {}
    for provider_id in (OPENAI_PROVIDER_ID, OPENAI_COMPATIBLE_PROVIDER_ID):
        descriptor = openai_compatible_descriptor(provider_id)

        def factory(
            settings: ProviderSettings,
            *,
            current_provider_id: str = provider_id,
        ) -> AIProvider:
            transport_factory = injected.get(current_provider_id)
            transport = (
                transport_factory()
                if transport_factory is not None
                else None
            )
            try:
                return provider_from_settings(settings, transport=transport)
            except ValueError as exc:
                raise ProviderUnavailableError(str(exc)) from exc

        registry.register(
            ProviderRegistration(
                descriptor=descriptor,
                factory=factory,
            )
        )
    return registry


@dataclass(slots=True)
class ConfiguredAIRuntime:
    """Selected provider plus the existing resilient execution boundary."""

    selection: ProviderSelection
    executor: ResilientAIExecutor

    @classmethod
    def from_settings(
        cls,
        registry: ProviderRegistry,
        settings: ProviderSettings,
        *,
        required_capabilities: frozenset[ModelCapability] = frozenset(
            {ModelCapability.TEXT_GENERATION}
        ),
        allow_deterministic_fallback: bool = False,
    ) -> "ConfiguredAIRuntime":
        selection = registry.resolve(
            settings,
            required_capabilities=required_capabilities,
            allow_deterministic_fallback=allow_deterministic_fallback,
        )
        return cls(
            selection=selection,
            executor=ResilientAIExecutor(
                selection.provider,
                retry_policy=settings.retry_policy,
                budget=settings.usage_budget,
            ),
        )

    async def generate(self, request: ModelRequest) -> ExecutionResult:
        routed_request = replace(request, model=self.selection.selected_model)
        return await self.executor.generate(routed_request)

    async def close(self) -> None:
        await self.selection.provider.close()
