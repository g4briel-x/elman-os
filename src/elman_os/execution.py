"""Bounded and resilient execution for provider-neutral AI requests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .provider import (
    AIProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
)


class AIExecutionErrorCode(StrEnum):
    """Kernel-level failures produced around, rather than by, a provider."""

    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RETRY_EXHAUSTED = "retry_exhausted"
    PROVIDER_CONTRACT = "provider_contract"


class AIExecutionError(RuntimeError):
    """Safe orchestration error that never embeds credentials or raw payloads."""

    def __init__(
        self,
        code: AIExecutionErrorCode,
        message: str,
        *,
        provider_id: str,
        attempts: int,
        last_provider_code: ProviderErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.attempts = attempts
        self.last_provider_code = last_provider_code


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential retry policy applied only to explicitly retryable failures."""

    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts doit être compris entre 1 et 10")
        if not 0.0 <= self.initial_delay_seconds <= 60.0:
            raise ValueError(
                "initial_delay_seconds doit être compris entre 0 et 60"
            )
        if not 0.0 <= self.max_delay_seconds <= 300.0:
            raise ValueError("max_delay_seconds doit être compris entre 0 et 300")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError(
                "initial_delay_seconds ne peut pas dépasser max_delay_seconds"
            )
        if not 1.0 <= self.backoff_multiplier <= 10.0:
            raise ValueError("backoff_multiplier doit être compris entre 1 et 10")

    def delay_for(self, failed_attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay_seconds)
        exponential = self.initial_delay_seconds * (
            self.backoff_multiplier ** max(0, failed_attempt - 1)
        )
        return min(exponential, self.max_delay_seconds)


@dataclass(frozen=True, slots=True)
class UsageBudget:
    """Hard limits shared by all calls made through one executor."""

    max_provider_calls: int = 10
    max_total_tokens: int = 100_000
    max_elapsed_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_provider_calls <= 10_000:
            raise ValueError("max_provider_calls doit être compris entre 1 et 10 000")
        if not 1 <= self.max_total_tokens <= 100_000_000:
            raise ValueError("max_total_tokens doit être compris entre 1 et 100 000 000")
        if not 0.0 < self.max_elapsed_seconds <= 86_400.0:
            raise ValueError(
                "max_elapsed_seconds doit être compris entre 0 et 86 400"
            )


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    provider_calls: int
    total_tokens: int
    elapsed_seconds: float


@dataclass(slots=True)
class BudgetLedger:
    """Mutable counters owned by one executor; no global process state."""

    budget: UsageBudget
    clock: Callable[[], float] = time.monotonic
    provider_calls: int = field(default=0, init=False)
    total_tokens: int = field(default=0, init=False)
    _started_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.budget.max_elapsed_seconds - self.elapsed_seconds)

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            provider_calls=self.provider_calls,
            total_tokens=self.total_tokens,
            elapsed_seconds=self.elapsed_seconds,
        )

    def assert_can_attempt(self, request: ModelRequest, provider_id: str) -> None:
        if self.remaining_seconds <= 0:
            self._raise_budget(provider_id, "Le budget de durée IA est épuisé")
        if self.provider_calls >= self.budget.max_provider_calls:
            self._raise_budget(provider_id, "Le budget d'appels IA est épuisé")

        estimated_input_tokens = sum(
            len(message.content.split()) for message in request.messages
        )
        maximum_request_tokens = estimated_input_tokens + request.max_output_tokens
        remaining_tokens = self.budget.max_total_tokens - self.total_tokens
        if maximum_request_tokens > remaining_tokens:
            self._raise_budget(
                provider_id,
                "Le plafond de tokens restant ne couvre pas la requête",
            )

    def record_provider_call(self) -> None:
        self.provider_calls += 1

    def record_response(self, response: ModelResponse, provider_id: str) -> None:
        self.total_tokens += response.usage.total_tokens
        if self.total_tokens > self.budget.max_total_tokens:
            self._raise_budget(provider_id, "Le budget total de tokens est dépassé")

    def _raise_budget(self, provider_id: str, message: str) -> None:
        raise AIExecutionError(
            AIExecutionErrorCode.BUDGET_EXCEEDED,
            message,
            provider_id=provider_id,
            attempts=self.provider_calls,
        )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    response: ModelResponse
    attempts: int
    usage: UsageSnapshot


@dataclass(slots=True)
class ResilientAIExecutor:
    """Execute provider calls with deadlines, bounded retries and hard budgets."""

    provider: AIProvider
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    budget: UsageBudget = field(default_factory=UsageBudget)
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    ledger: BudgetLedger = field(init=False)

    def __post_init__(self) -> None:
        self.ledger = BudgetLedger(self.budget, self.clock)

    async def generate(self, request: ModelRequest) -> ExecutionResult:
        provider_id = self.provider.descriptor.provider_id
        request_deadline = self.clock() + request.timeout_seconds
        last_error: ProviderError | None = None

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            self.ledger.assert_can_attempt(request, provider_id)
            timeout = min(
                request_deadline - self.clock(),
                self.ledger.remaining_seconds,
            )
            if timeout <= 0:
                raise AIExecutionError(
                    AIExecutionErrorCode.DEADLINE_EXCEEDED,
                    "Le délai global de la requête IA est épuisé",
                    provider_id=provider_id,
                    attempts=attempt - 1,
                    last_provider_code=(
                        last_error.code if last_error is not None else None
                    ),
                )

            self.ledger.record_provider_call()
            try:
                async with asyncio.timeout(timeout):
                    response = await self.provider.generate(request)
            except TimeoutError:
                last_error = ProviderError(
                    ProviderErrorCode.TIMEOUT,
                    "Le fournisseur IA n'a pas répondu dans le délai imparti",
                    provider_id=provider_id,
                    retryable=True,
                )
            except ProviderError as exc:
                last_error = exc
            else:
                self._validate_response(request, response, attempt)
                self.ledger.record_response(response, provider_id)
                return ExecutionResult(
                    response=response,
                    attempts=attempt,
                    usage=self.ledger.snapshot(),
                )

            if not last_error.retryable:
                raise last_error
            if attempt >= self.retry_policy.max_attempts:
                raise AIExecutionError(
                    AIExecutionErrorCode.RETRY_EXHAUSTED,
                    "Toutes les tentatives IA autorisées ont échoué",
                    provider_id=provider_id,
                    attempts=attempt,
                    last_provider_code=last_error.code,
                ) from last_error

            delay = self.retry_policy.delay_for(
                attempt,
                last_error.retry_after_seconds,
            )
            remaining = min(
                request_deadline - self.clock(),
                self.ledger.remaining_seconds,
            )
            if delay >= remaining:
                raise AIExecutionError(
                    AIExecutionErrorCode.DEADLINE_EXCEEDED,
                    "Le délai restant est insuffisant pour une nouvelle tentative",
                    provider_id=provider_id,
                    attempts=attempt,
                    last_provider_code=last_error.code,
                ) from last_error
            if delay > 0:
                await self.sleeper(delay)

        raise AssertionError("La boucle de retry doit toujours retourner ou lever")

    def _validate_response(
        self,
        request: ModelRequest,
        response: ModelResponse,
        attempt: int,
    ) -> None:
        provider_id = self.provider.descriptor.provider_id
        valid = (
            response.request_id == request.request_id
            and response.provider_id == provider_id
            and response.model == request.model
            and response.usage.output_tokens <= request.max_output_tokens
        )
        if not valid:
            raise AIExecutionError(
                AIExecutionErrorCode.PROVIDER_CONTRACT,
                "Le fournisseur IA a renvoyé une réponse hors contrat",
                provider_id=provider_id,
                attempts=attempt,
            )
