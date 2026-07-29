"""Provider-neutral contracts for model-backed ELMAN-OS agents.

The generic AI contract intentionally contains no vendor SDK and performs no
network call. Provider-specific adapters translate these stable types to their
own APIs at the boundary of the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .domain import AgentOutput, AgentProfile, Evidence, TaskEnvelope


class ModelCapability(StrEnum):
    """Optional features an AI provider can declare."""

    TEXT_GENERATION = "text_generation"
    JSON_OUTPUT = "json_output"
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    VISION = "vision"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CONTENT_FILTERED = "content_filtered"
    SERVICE_UNAVAILABLE = "service_unavailable"
    NETWORK = "network"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Identity and declared capabilities of one configured provider."""

    provider_id: str
    display_name: str
    capabilities: frozenset[ModelCapability]
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id ne peut pas être vide")
        if not self.display_name.strip():
            raise ValueError("display_name ne peut pas être vide")
        if any(not model.strip() for model in self.models):
            raise ValueError("models ne peut pas contenir un identifiant vide")

    def supports(self, capability: ModelCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Le contenu d'un message ne peut pas être vide")
        if self.name is not None and not self.name.strip():
            raise ValueError("Le nom d'un message ne peut pas être vide")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Portable text-generation request passed to every provider adapter."""

    request_id: str
    model: str
    messages: tuple[ModelMessage, ...]
    max_output_tokens: int = 2_048
    temperature: float = 0.2
    timeout_seconds: float = 60.0
    required_capabilities: frozenset[ModelCapability] = field(
        default_factory=lambda: frozenset({ModelCapability.TEXT_GENERATION})
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id ne peut pas être vide")
        if not self.model.strip():
            raise ValueError("model ne peut pas être vide")
        if not self.messages:
            raise ValueError("Une requête doit contenir au moins un message")
        if not 1 <= self.max_output_tokens <= 1_000_000:
            raise ValueError(
                "max_output_tokens doit être compris entre 1 et 1 000 000"
            )
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature doit être comprise entre 0 et 2")
        if not 0.0 < self.timeout_seconds <= 600.0:
            raise ValueError("timeout_seconds doit être compris entre 0 et 600")
        if ModelCapability.TEXT_GENERATION not in self.required_capabilities:
            raise ValueError("TEXT_GENERATION est requis pour ModelRequest")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("Le nombre de tokens ne peut pas être négatif")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    provider_id: str
    model: str
    content: str
    finish_reason: FinishReason
    usage: TokenUsage
    provider_request_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id ne peut pas être vide")
        if not self.provider_id.strip():
            raise ValueError("provider_id ne peut pas être vide")
        if not self.model.strip():
            raise ValueError("model ne peut pas être vide")
        if not self.content and self.finish_reason not in {
            FinishReason.CONTENT_FILTER,
            FinishReason.CANCELLED,
            FinishReason.TOOL_CALL,
        }:
            raise ValueError("Une réponse terminée doit contenir du texte")


class ProviderError(RuntimeError):
    """Normalized provider failure safe for orchestration decisions."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        provider_id: str,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        if not message.strip():
            raise ValueError("Le message d'erreur ne peut pas être vide")
        if not provider_id.strip():
            raise ValueError("provider_id ne peut pas être vide")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds ne peut pas être négatif")
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class AIProvider(Protocol):
    """Minimal asynchronous contract implemented by every AI adapter."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Return provider identity and supported capabilities."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Execute one bounded text-generation request."""

    async def close(self) -> None:
        """Release provider-owned transports or sessions."""


@dataclass(slots=True)
class DeterministicModelProvider:
    """No-network provider for contract tests and local development."""

    responses: Sequence[str] = ("Réponse simulée ELMAN-OS.",)
    provider_id: str = "deterministic-model"
    _request_count: int = field(default=0, init=False, repr=False)
    requests: list[ModelRequest] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            display_name="ELMAN deterministic model provider",
            capabilities=frozenset({ModelCapability.TEXT_GENERATION}),
            models=("deterministic-v1",),
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self.closed:
            raise ProviderError(
                ProviderErrorCode.SERVICE_UNAVAILABLE,
                "Le fournisseur est fermé",
                provider_id=self.provider_id,
            )
        missing = request.required_capabilities - self.descriptor.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"Capacités non prises en charge : {names}",
                provider_id=self.provider_id,
            )
        if (
            self.descriptor.models
            and request.model not in self.descriptor.models
        ):
            raise ProviderError(
                ProviderErrorCode.MODEL_NOT_FOUND,
                f"Modèle non pris en charge : {request.model}",
                provider_id=self.provider_id,
            )
        if not self.responses:
            raise ProviderError(
                ProviderErrorCode.SERVICE_UNAVAILABLE,
                "Aucune réponse simulée n'est configurée",
                provider_id=self.provider_id,
            )

        self.requests.append(request)
        response_index = min(self._request_count, len(self.responses) - 1)
        content = self.responses[response_index]
        self._request_count += 1
        input_tokens = sum(
            len(message.content.split()) for message in request.messages
        )
        output_tokens = len(content.split())
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=request.model,
            content=content,
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            provider_request_id=f"det-{self._request_count:06d}",
        )

    async def close(self) -> None:
        self.closed = True


class AgentProvider(Protocol):
    async def run(self, agent: AgentProfile, task: TaskEnvelope) -> AgentOutput:
        """Execute one bounded agent task."""


@dataclass(slots=True)
class DeterministicDemoProvider:
    """Safe provider used for tests and local demonstrations.

    It does not call a language model and must not be mistaken for a production
    generation provider.
    """

    label: str = "deterministic-demo"

    async def run(self, agent: AgentProfile, task: TaskEnvelope) -> AgentOutput:
        return AgentOutput(
            agent_id=agent.agent_id,
            task_id=task.task_id,
            summary=f"{agent.name} a traité la tâche de démonstration.",
            evidence=[
                Evidence(
                    claim="Exécution déterministe terminée",
                    source=self.label,
                    observed=True,
                )
            ],
            confidence="high",
        )
