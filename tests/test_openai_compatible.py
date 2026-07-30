import json
import unittest
from dataclasses import dataclass, field

from elman_os.configuration import ProviderSettings, SecretValue
from elman_os.openai_compatible import (
    HTTPRequest,
    HTTPResponse,
    HTTPTransportError,
    OPENAI_DEFAULT_BASE_URL,
    OpenAICompatibleProvider,
    provider_from_settings,
)
from elman_os.provider import (
    FinishReason,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ProviderError,
    ProviderErrorCode,
)
from elman_os.registry import ConfiguredAIRuntime, built_in_provider_registry


def request() -> ModelRequest:
    return ModelRequest(
        request_id="req-offline-001",
        model="gpt-test",
        messages=(
            ModelMessage(MessageRole.SYSTEM, "Répondre brièvement."),
            ModelMessage(MessageRole.USER, "Bonjour", name="tester"),
        ),
        max_output_tokens=25,
        temperature=0.1,
    )


def success_response() -> HTTPResponse:
    return HTTPResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "id": "chatcmpl-test",
                "model": "gpt-test",
                "choices": [
                    {
                        "message": {"content": "Réponse hors réseau."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 4,
                },
            }
        ).encode(),
    )


@dataclass(slots=True)
class FakeTransport:
    responses: list[HTTPResponse] = field(default_factory=list)
    error: Exception | None = None
    requests: list[HTTPRequest] = field(default_factory=list)
    closed: bool = False

    async def send(self, outgoing: HTTPRequest) -> HTTPResponse:
        self.requests.append(outgoing)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def provider(
    transport: FakeTransport,
    provider_id: str = "openai-compatible",
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_id=provider_id,
        api_key=SecretValue("offline-fake-key"),
        base_url="https://example.invalid/v1/",
        transport=transport,
    )


class OpenAICompatibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_translation_and_response_mapping(self) -> None:
        transport = FakeTransport([success_response()])
        result = await provider(transport).generate(request())
        outgoing = transport.requests[0]
        payload = json.loads(outgoing.body)

        self.assertEqual(outgoing.method, "POST")
        self.assertEqual(
            outgoing.url,
            "https://example.invalid/v1/chat/completions",
        )
        self.assertEqual(outgoing.headers["Authorization"], "Bearer offline-fake-key")
        self.assertNotIn("offline-fake-key", repr(outgoing))
        self.assertEqual(outgoing.headers["X-Request-ID"], "req-offline-001")
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["messages"][1]["name"], "tester")
        self.assertEqual(payload["max_tokens"], 25)
        self.assertEqual(result.content, "Réponse hors réseau.")
        self.assertEqual(result.finish_reason, FinishReason.STOP)
        self.assertEqual(result.usage.total_tokens, 11)
        self.assertEqual(result.provider_request_id, "chatcmpl-test")

    async def test_adapter_repr_never_contains_key(self) -> None:
        adapter = provider(FakeTransport())
        self.assertNotIn("offline-fake-key", repr(adapter))
        self.assertNotIn("offline-fake-key", str(adapter))

    async def test_close_is_idempotent_and_blocks_generation(self) -> None:
        transport = FakeTransport([success_response()])
        adapter = provider(transport)
        await adapter.close()
        await adapter.close()
        self.assertTrue(transport.closed)
        with self.assertRaises(ProviderError) as raised:
            await adapter.generate(request())
        self.assertEqual(
            raised.exception.code,
            ProviderErrorCode.SERVICE_UNAVAILABLE,
        )

    async def test_unsupported_capability_is_rejected_before_transport(self) -> None:
        transport = FakeTransport([success_response()])
        unsupported = ModelRequest(
            request_id="vision-1",
            model="gpt-test",
            messages=(ModelMessage(MessageRole.USER, "Image"),),
            required_capabilities=frozenset(
                {ModelCapability.TEXT_GENERATION, ModelCapability.VISION}
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(unsupported)
        self.assertEqual(raised.exception.code, ProviderErrorCode.INVALID_REQUEST)
        self.assertEqual(transport.requests, [])

    async def test_network_failure_is_retryable_and_sanitized(self) -> None:
        transport = FakeTransport(
            error=HTTPTransportError("offline-fake-key must not escape")
        )
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.NETWORK)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("offline-fake-key", str(raised.exception))

    async def test_authentication_error_is_not_retryable(self) -> None:
        transport = FakeTransport(
            [HTTPResponse(401, body=b'{"error":"offline-fake-key"}')]
        )
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.AUTHENTICATION)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("offline-fake-key", str(raised.exception))

    async def test_rate_limit_parses_bounded_retry_after(self) -> None:
        transport = FakeTransport(
            [HTTPResponse(429, {"Retry-After": "1.5"}, b"ignored")]
        )
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.RATE_LIMITED)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after_seconds, 1.5)

    async def test_server_error_is_retryable(self) -> None:
        transport = FakeTransport([HTTPResponse(503)])
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(
            raised.exception.code,
            ProviderErrorCode.SERVICE_UNAVAILABLE,
        )
        self.assertTrue(raised.exception.retryable)

    async def test_invalid_json_is_normalized(self) -> None:
        transport = FakeTransport([HTTPResponse(200, body=b"not-json")])
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.UNKNOWN)

    async def test_missing_choice_is_normalized(self) -> None:
        transport = FakeTransport([HTTPResponse(200, body=b'{"choices":[]}')])
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.UNKNOWN)

    async def test_negative_usage_is_rejected(self) -> None:
        raw = json.loads(success_response().body)
        raw["usage"]["prompt_tokens"] = -1
        transport = FakeTransport([HTTPResponse(200, body=json.dumps(raw).encode())])
        with self.assertRaises(ProviderError) as raised:
            await provider(transport).generate(request())
        self.assertEqual(raised.exception.code, ProviderErrorCode.UNKNOWN)

    async def test_unknown_finish_reason_is_portable(self) -> None:
        raw = json.loads(success_response().body)
        raw["choices"][0]["finish_reason"] = "vendor-specific"
        transport = FakeTransport([HTTPResponse(200, body=json.dumps(raw).encode())])
        result = await provider(transport).generate(request())
        self.assertEqual(result.finish_reason, FinishReason.UNKNOWN)

    async def test_empty_filtered_response_is_supported(self) -> None:
        raw = json.loads(success_response().body)
        raw["choices"][0]["message"]["content"] = ""
        raw["choices"][0]["finish_reason"] = "content_filter"
        transport = FakeTransport([HTTPResponse(200, body=json.dumps(raw).encode())])
        result = await provider(transport).generate(request())
        self.assertEqual(result.content, "")
        self.assertEqual(result.finish_reason, FinishReason.CONTENT_FILTER)


class OpenAICompatibleFactoryTests(unittest.TestCase):
    def test_openai_uses_official_default_base_url(self) -> None:
        adapter = provider_from_settings(
            ProviderSettings(
                provider_id="openai",
                model="gpt-test",
                auth_mode="api_key",
                api_key=SecretValue("fake"),
            ),
            transport=FakeTransport(),
        )
        self.assertEqual(adapter.base_url, OPENAI_DEFAULT_BASE_URL)

    def test_openai_compatible_requires_explicit_base_url(self) -> None:
        settings = ProviderSettings(
            provider_id="openai-compatible",
            model="gpt-test",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        with self.assertRaises(ValueError):
            provider_from_settings(settings, transport=FakeTransport())

    def test_factory_rejects_wrong_provider(self) -> None:
        settings = ProviderSettings(
            provider_id="other",
            model="gpt-test",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        with self.assertRaises(ValueError):
            provider_from_settings(settings, transport=FakeTransport())

    def test_registry_declares_offline_safe_adapters(self) -> None:
        self.assertEqual(
            [item.provider_id for item in built_in_provider_registry().descriptors()],
            ["deterministic-model", "openai", "openai-compatible"],
        )


class OpenAICompatiblePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_to_executor_pipeline_uses_injected_transport(self) -> None:
        transport = FakeTransport([success_response()])
        registry = built_in_provider_registry(
            transport_factories={
                "openai-compatible": lambda: transport,
            }
        )
        settings = ProviderSettings(
            provider_id="openai-compatible",
            model="gpt-test",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
            base_url="https://example.invalid/v1",
        )
        runtime = ConfiguredAIRuntime.from_settings(registry, settings)
        result = await runtime.generate(request())
        self.assertEqual(result.response.provider_id, "openai-compatible")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(transport.requests), 1)
        await runtime.close()
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
