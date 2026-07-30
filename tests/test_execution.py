import asyncio
import unittest
from dataclasses import dataclass, field

from elman_os.execution import (
    AIExecutionError,
    AIExecutionErrorCode,
    ResilientAIExecutor,
    RetryPolicy,
    UsageBudget,
)
from elman_os.provider import (
    FinishReason,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderDescriptor,
    ProviderError,
    ProviderErrorCode,
    TokenUsage,
)


def request(
    request_id: str = "req-001",
    *,
    timeout_seconds: float = 1.0,
    max_output_tokens: int = 10,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        model="scripted-v1",
        messages=(ModelMessage(MessageRole.USER, "Bonjour ELMAN"),),
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def response(
    request_id: str = "req-001",
    *,
    provider_id: str = "scripted",
    model: str = "scripted-v1",
    input_tokens: int = 2,
    output_tokens: int = 2,
) -> ModelResponse:
    return ModelResponse(
        request_id=request_id,
        provider_id=provider_id,
        model=model,
        content="Réponse valide",
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens, output_tokens),
    )


@dataclass(slots=True)
class ScriptedProvider:
    outcomes: list[ModelResponse | BaseException]
    calls: int = field(default=0, init=False)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id="scripted",
            display_name="Scripted test provider",
            capabilities=frozenset({ModelCapability.TEXT_GENERATION}),
            models=("scripted-v1",),
        )

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


def temporary_error(
    *,
    retry_after_seconds: float | None = None,
) -> ProviderError:
    return ProviderError(
        ProviderErrorCode.SERVICE_UNAVAILABLE,
        "Panne temporaire",
        provider_id="scripted",
        retryable=True,
        retry_after_seconds=retry_after_seconds,
    )


class ResilientExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_attempt_and_usage_snapshot(self) -> None:
        provider = ScriptedProvider([response()])
        executor = ResilientAIExecutor(provider)

        result = await executor.generate(request())

        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.response.content, "Réponse valide")
        self.assertEqual(result.usage.provider_calls, 1)
        self.assertEqual(result.usage.total_tokens, 4)

    async def test_retryable_error_is_retried_then_succeeds(self) -> None:
        provider = ScriptedProvider([temporary_error(), response()])
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        executor = ResilientAIExecutor(
            provider,
            RetryPolicy(max_attempts=3, initial_delay_seconds=0.1),
            sleeper=record_sleep,
        )

        result = await executor.generate(request())

        self.assertEqual(result.attempts, 2)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(delays, [0.1])

    async def test_retry_after_is_honored_and_bounded(self) -> None:
        provider = ScriptedProvider(
            [temporary_error(retry_after_seconds=30), response()]
        )
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        executor = ResilientAIExecutor(
            provider,
            RetryPolicy(max_attempts=2, max_delay_seconds=2),
            sleeper=record_sleep,
        )

        await executor.generate(request(timeout_seconds=10))

        self.assertEqual(delays, [2])

    async def test_non_retryable_error_fails_immediately(self) -> None:
        error = ProviderError(
            ProviderErrorCode.AUTHENTICATION,
            "Authentification refusée",
            provider_id="scripted",
            retryable=False,
        )
        provider = ScriptedProvider([error])
        executor = ResilientAIExecutor(provider)

        with self.assertRaises(ProviderError) as raised:
            await executor.generate(request())

        self.assertEqual(raised.exception.code, ProviderErrorCode.AUTHENTICATION)
        self.assertEqual(provider.calls, 1)

    async def test_retry_exhaustion_is_normalized(self) -> None:
        provider = ScriptedProvider([temporary_error()])
        executor = ResilientAIExecutor(
            provider,
            RetryPolicy(
                max_attempts=2,
                initial_delay_seconds=0,
                max_delay_seconds=0,
            ),
        )

        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request())

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.RETRY_EXHAUSTED,
        )
        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(
            raised.exception.last_provider_code,
            ProviderErrorCode.SERVICE_UNAVAILABLE,
        )

    async def test_timeout_is_normalized_and_bounded(self) -> None:
        class SlowProvider(ScriptedProvider):
            async def generate(self, model_request: ModelRequest) -> ModelResponse:
                self.calls += 1
                await asyncio.sleep(1)
                return response(model_request.request_id)

        provider = SlowProvider([response()])
        executor = ResilientAIExecutor(
            provider,
            RetryPolicy(max_attempts=1),
        )

        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request(timeout_seconds=0.01))

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.RETRY_EXHAUSTED,
        )
        self.assertEqual(
            raised.exception.last_provider_code,
            ProviderErrorCode.TIMEOUT,
        )

    async def test_cancellation_is_never_converted_to_retry(self) -> None:
        provider = ScriptedProvider([asyncio.CancelledError()])
        executor = ResilientAIExecutor(provider)

        with self.assertRaises(asyncio.CancelledError):
            await executor.generate(request())

        self.assertEqual(provider.calls, 1)

    async def test_provider_call_budget_is_shared_across_requests(self) -> None:
        provider = ScriptedProvider([response(), response("req-002")])
        executor = ResilientAIExecutor(
            provider,
            budget=UsageBudget(
                max_provider_calls=1,
                max_total_tokens=100,
                max_elapsed_seconds=10,
            ),
        )

        await executor.generate(request())
        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request("req-002"))

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.BUDGET_EXCEEDED,
        )
        self.assertEqual(provider.calls, 1)

    async def test_token_budget_blocks_call_before_provider_execution(self) -> None:
        provider = ScriptedProvider([response()])
        executor = ResilientAIExecutor(
            provider,
            budget=UsageBudget(
                max_provider_calls=2,
                max_total_tokens=5,
                max_elapsed_seconds=10,
            ),
        )

        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request(max_output_tokens=4))

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.BUDGET_EXCEEDED,
        )
        self.assertEqual(provider.calls, 0)

    async def test_response_identity_is_checked(self) -> None:
        provider = ScriptedProvider([response(request_id="wrong")])
        executor = ResilientAIExecutor(provider)

        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request())

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.PROVIDER_CONTRACT,
        )

    async def test_output_token_limit_is_checked(self) -> None:
        provider = ScriptedProvider([response(output_tokens=11)])
        executor = ResilientAIExecutor(provider)

        with self.assertRaises(AIExecutionError) as raised:
            await executor.generate(request(max_output_tokens=10))

        self.assertEqual(
            raised.exception.code,
            AIExecutionErrorCode.PROVIDER_CONTRACT,
        )


class RuntimePolicyValidationTests(unittest.TestCase):
    def test_retry_policy_rejects_unbounded_attempts(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=11)

    def test_retry_policy_rejects_inverted_delays(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(initial_delay_seconds=2, max_delay_seconds=1)

    def test_usage_budget_requires_positive_limits(self) -> None:
        with self.assertRaises(ValueError):
            UsageBudget(max_provider_calls=0)


if __name__ == "__main__":
    unittest.main()
