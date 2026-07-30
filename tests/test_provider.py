import unittest

from elman_os.provider import (
    AIProvider,
    DeterministicModelProvider,
    FinishReason,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ProviderError,
    ProviderErrorCode,
)


def request(
    *,
    model: str = "deterministic-v1",
    capabilities: frozenset[ModelCapability] | None = None,
) -> ModelRequest:
    return ModelRequest(
        request_id="req-001",
        model=model,
        messages=(
            ModelMessage(MessageRole.SYSTEM, "Répondre brièvement."),
            ModelMessage(MessageRole.USER, "Décrire le contrat."),
        ),
        required_capabilities=capabilities
        or frozenset({ModelCapability.TEXT_GENERATION}),
    )


class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_provider_satisfies_runtime_contract(self) -> None:
        provider = DeterministicModelProvider(("Contrat validé.",))

        self.assertIsInstance(provider, AIProvider)
        response = await provider.generate(request())

        self.assertEqual(response.request_id, "req-001")
        self.assertEqual(response.content, "Contrat validé.")
        self.assertEqual(response.finish_reason, FinishReason.STOP)
        self.assertEqual(response.usage.total_tokens, 7)
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(provider.closed)

        await provider.close()
        self.assertTrue(provider.closed)

    async def test_provider_rejects_unknown_model(self) -> None:
        provider = DeterministicModelProvider()

        with self.assertRaises(ProviderError) as raised:
            await provider.generate(request(model="unknown"))

        self.assertEqual(raised.exception.code, ProviderErrorCode.MODEL_NOT_FOUND)
        self.assertFalse(raised.exception.retryable)

    async def test_provider_fails_closed_for_missing_capability(self) -> None:
        provider = DeterministicModelProvider()

        with self.assertRaises(ProviderError) as raised:
            await provider.generate(
                request(
                    capabilities=frozenset(
                        {
                            ModelCapability.TEXT_GENERATION,
                            ModelCapability.TOOL_CALLING,
                        }
                    )
                )
            )

        self.assertEqual(raised.exception.code, ProviderErrorCode.INVALID_REQUEST)

    async def test_closed_provider_cannot_be_reused(self) -> None:
        provider = DeterministicModelProvider()
        await provider.close()

        with self.assertRaises(ProviderError) as raised:
            await provider.generate(request())

        self.assertEqual(
            raised.exception.code,
            ProviderErrorCode.SERVICE_UNAVAILABLE,
        )


class ProviderValidationTests(unittest.TestCase):
    def test_request_bounds_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            ModelRequest(
                request_id="req-invalid",
                model="deterministic-v1",
                messages=(ModelMessage(MessageRole.USER, "Test"),),
                temperature=2.1,
            )

    def test_empty_message_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ModelMessage(MessageRole.USER, "   ")

    def test_text_generation_capability_is_mandatory(self) -> None:
        with self.assertRaises(ValueError):
            request(
                capabilities=frozenset({ModelCapability.JSON_OUTPUT})
            )

    def test_retry_metadata_is_typed(self) -> None:
        error = ProviderError(
            ProviderErrorCode.RATE_LIMITED,
            "Limite temporaire",
            provider_id="vendor",
            retryable=True,
            retry_after_seconds=2.5,
        )

        self.assertTrue(error.retryable)
        self.assertEqual(error.retry_after_seconds, 2.5)


if __name__ == "__main__":
    unittest.main()
