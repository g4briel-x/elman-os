import unittest
from dataclasses import dataclass

from elman_os.configuration import ProviderSettings, SecretValue
from elman_os.provider import (
    DeterministicModelProvider,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ProviderDescriptor,
)
from elman_os.registry import (
    ConfiguredAIRuntime,
    ProviderRegistration,
    ProviderRegistry,
    ProviderRegistryError,
    ProviderUnavailableError,
    RegistryErrorCode,
    built_in_provider_registry,
)


def deterministic_settings() -> ProviderSettings:
    return ProviderSettings(
        provider_id="deterministic-model",
        model="deterministic-v1",
        auth_mode="none",
    )


def remote_settings() -> ProviderSettings:
    return ProviderSettings(
        provider_id="remote-test",
        model="remote-v1",
        auth_mode="api_key",
        api_key=SecretValue("not-a-real-secret"),
        base_url="https://example.invalid/v1",
    )


def remote_descriptor(
    capabilities: frozenset[ModelCapability] = frozenset(
        {ModelCapability.TEXT_GENERATION}
    ),
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="remote-test",
        display_name="Remote test adapter",
        capabilities=capabilities,
        models=("remote-v1",),
    )


@dataclass(slots=True)
class InvalidProvider:
    descriptor: ProviderDescriptor


class RegistryTests(unittest.TestCase):
    def test_builtin_registry_contains_only_deterministic_provider(self) -> None:
        descriptors = built_in_provider_registry().descriptors()
        self.assertEqual(
            [item.provider_id for item in descriptors],
            ["deterministic-model"],
        )

    def test_registration_is_visible_through_read_only_mapping(self) -> None:
        registry = built_in_provider_registry()
        self.assertIn("deterministic-model", registry.registrations)
        with self.assertRaises(TypeError):
            registry.registrations["other"] = object()  # type: ignore[index]

    def test_duplicate_provider_is_rejected(self) -> None:
        registry = built_in_provider_registry()
        provider = DeterministicModelProvider()
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.register(
                ProviderRegistration(
                    provider.descriptor,
                    lambda settings: provider,
                )
            )
        self.assertEqual(raised.exception.code, RegistryErrorCode.DUPLICATE_PROVIDER)

    def test_descriptors_are_sorted(self) -> None:
        registry = built_in_provider_registry()
        registry.register(
            ProviderRegistration(
                remote_descriptor(),
                lambda settings: DeterministicModelProvider(
                    provider_id="remote-test"
                ),
            )
        )
        self.assertEqual(
            [item.provider_id for item in registry.descriptors()],
            ["deterministic-model", "remote-test"],
        )

    def test_factory_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            ProviderRegistration(remote_descriptor(), None)  # type: ignore[arg-type]

    def test_configured_provider_is_selected(self) -> None:
        registry = built_in_provider_registry()
        provider = DeterministicModelProvider(provider_id="remote-test")
        provider.descriptor  # exercise descriptor before registration
        descriptor = ProviderDescriptor(
            "remote-test",
            "ELMAN deterministic model provider",
            frozenset({ModelCapability.TEXT_GENERATION}),
            ("deterministic-v1",),
        )
        registry.register(
            ProviderRegistration(descriptor, lambda settings: provider)
        )
        settings = ProviderSettings(
            provider_id="remote-test",
            model="deterministic-v1",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        selection = registry.resolve(settings)
        self.assertFalse(selection.used_fallback)
        self.assertIs(selection.provider, provider)

    def test_unknown_provider_fails_closed_by_default(self) -> None:
        registry = built_in_provider_registry()
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(remote_settings())
        self.assertEqual(raised.exception.code, RegistryErrorCode.PROVIDER_NOT_FOUND)

    def test_unknown_provider_can_use_explicit_fallback(self) -> None:
        selection = built_in_provider_registry().resolve(
            remote_settings(),
            allow_deterministic_fallback=True,
        )
        self.assertTrue(selection.used_fallback)
        self.assertEqual(selection.selected_provider_id, "deterministic-model")
        self.assertEqual(selection.selected_model, "deterministic-v1")
        self.assertEqual(
            selection.fallback_reason,
            RegistryErrorCode.PROVIDER_NOT_FOUND,
        )

    def test_unavailable_factory_fails_closed_by_default(self) -> None:
        registry = built_in_provider_registry()

        def unavailable(settings: ProviderSettings):
            raise ProviderUnavailableError("offline")

        registry.register(ProviderRegistration(remote_descriptor(), unavailable))
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(remote_settings())
        self.assertEqual(
            raised.exception.code,
            RegistryErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_unavailable_factory_can_use_explicit_fallback(self) -> None:
        registry = built_in_provider_registry()

        def unavailable(settings: ProviderSettings):
            raise ProviderUnavailableError("offline")

        registry.register(ProviderRegistration(remote_descriptor(), unavailable))
        selection = registry.resolve(
            remote_settings(),
            allow_deterministic_fallback=True,
        )
        self.assertTrue(selection.used_fallback)
        self.assertEqual(
            selection.fallback_reason,
            RegistryErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_model_mismatch_never_uses_fallback(self) -> None:
        registry = built_in_provider_registry()
        registry.register(
            ProviderRegistration(
                remote_descriptor(),
                lambda settings: DeterministicModelProvider(),
            )
        )
        settings = ProviderSettings(
            provider_id="remote-test",
            model="other-model",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(settings, allow_deterministic_fallback=True)
        self.assertEqual(
            raised.exception.code,
            RegistryErrorCode.MODEL_NOT_SUPPORTED,
        )

    def test_capability_mismatch_never_uses_fallback(self) -> None:
        registry = built_in_provider_registry()
        registry.register(
            ProviderRegistration(
                remote_descriptor(),
                lambda settings: DeterministicModelProvider(),
            )
        )
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(
                remote_settings(),
                required_capabilities=frozenset(
                    {
                        ModelCapability.TEXT_GENERATION,
                        ModelCapability.VISION,
                    }
                ),
                allow_deterministic_fallback=True,
            )
        self.assertEqual(
            raised.exception.code,
            RegistryErrorCode.CAPABILITY_NOT_SUPPORTED,
        )

    def test_missing_fallback_registration_is_reported(self) -> None:
        registry = ProviderRegistry()
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(
                remote_settings(),
                allow_deterministic_fallback=True,
            )
        self.assertEqual(
            raised.exception.code,
            RegistryErrorCode.FALLBACK_NOT_AVAILABLE,
        )

    def test_invalid_factory_result_is_rejected(self) -> None:
        registry = built_in_provider_registry()
        registry.register(
            ProviderRegistration(
                remote_descriptor(),
                lambda settings: InvalidProvider(remote_descriptor()),
            )
        )
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(remote_settings())
        self.assertEqual(raised.exception.code, RegistryErrorCode.INVALID_PROVIDER)

    def test_descriptor_mismatch_is_rejected(self) -> None:
        registry = built_in_provider_registry()
        registry.register(
            ProviderRegistration(
                remote_descriptor(),
                lambda settings: DeterministicModelProvider(),
            )
        )
        with self.assertRaises(ProviderRegistryError) as raised:
            registry.resolve(remote_settings())
        self.assertEqual(
            raised.exception.code,
            RegistryErrorCode.DESCRIPTOR_MISMATCH,
        )

    def test_safe_summary_contains_no_api_key(self) -> None:
        selection = built_in_provider_registry().resolve(
            remote_settings(),
            allow_deterministic_fallback=True,
        )
        summary = repr(selection.safe_summary())
        self.assertNotIn("not-a-real-secret", summary)
        self.assertIn("used_fallback", summary)

    def test_registry_errors_are_portable(self) -> None:
        error = ProviderRegistryError(
            RegistryErrorCode.PROVIDER_NOT_FOUND,
            "Absent",
            provider_id="remote-test",
        )
        self.assertEqual(error.provider_id, "remote-test")
        self.assertEqual(error.code.value, "provider_not_found")


class ConfiguredRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_fallback_pipeline_is_deterministic(self) -> None:
        settings = remote_settings()
        runtime = ConfiguredAIRuntime.from_settings(
            built_in_provider_registry(),
            settings,
            allow_deterministic_fallback=True,
        )
        result = await runtime.generate(
            ModelRequest(
                request_id="pipeline-001",
                model=settings.model,
                messages=(ModelMessage(MessageRole.USER, "Tester le pipeline."),),
                max_output_tokens=20,
            )
        )
        self.assertEqual(result.response.provider_id, "deterministic-model")
        self.assertEqual(result.response.model, "deterministic-v1")
        self.assertEqual(result.attempts, 1)
        await runtime.close()


if __name__ == "__main__":
    unittest.main()
