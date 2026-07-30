import unittest

from elman_os.configuration import (
    ConfigurationError,
    SecretValue,
    load_provider_settings,
)


class SecureConfigurationTests(unittest.TestCase):
    def test_defaults_use_no_network_deterministic_provider(self) -> None:
        settings = load_provider_settings({})

        self.assertEqual(settings.provider_id, "deterministic-model")
        self.assertEqual(settings.model, "deterministic-v1")
        self.assertEqual(settings.auth_mode, "none")
        self.assertIsNone(settings.api_key)

    def test_real_provider_requires_api_key_by_default(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "ELMAN_AI_API_KEY"):
            load_provider_settings(
                {
                    "ELMAN_AI_PROVIDER": "vendor",
                    "ELMAN_AI_MODEL": "vendor-model",
                }
            )

    def test_defined_api_key_cannot_be_empty(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "ELMAN_AI_API_KEY"):
            load_provider_settings({"ELMAN_AI_API_KEY": "   "})

    def test_secret_is_redacted_from_string_and_repr(self) -> None:
        secret = SecretValue("highly-sensitive-value")

        self.assertEqual(str(secret), "***REDACTED***")
        self.assertNotIn("highly-sensitive-value", repr(secret))
        self.assertEqual(secret.reveal(), "highly-sensitive-value")

    def test_safe_summary_never_contains_secret(self) -> None:
        settings = load_provider_settings(
            {
                "ELMAN_AI_PROVIDER": "vendor",
                "ELMAN_AI_MODEL": "vendor/model-v1",
                "ELMAN_AI_API_KEY": "highly-sensitive-value",
            }
        )

        summary = settings.safe_summary()
        self.assertTrue(summary["credential_configured"])
        self.assertNotIn("highly-sensitive-value", repr(settings))
        self.assertNotIn("highly-sensitive-value", repr(summary))

    def test_api_key_is_forbidden_when_authentication_is_disabled(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_provider_settings(
                {
                    "ELMAN_AI_AUTH_MODE": "none",
                    "ELMAN_AI_API_KEY": "must-not-be-used",
                }
            )

    def test_invalid_numeric_value_fails_without_echoing_value(self) -> None:
        invalid_value = "secret-looking-invalid-number"

        with self.assertRaises(ConfigurationError) as raised:
            load_provider_settings(
                {"ELMAN_AI_TIMEOUT_SECONDS": invalid_value}
            )

        self.assertNotIn(invalid_value, str(raised.exception))

    def test_limits_are_bounded(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_provider_settings({"ELMAN_AI_MAX_OUTPUT_TOKENS": "0"})
        with self.assertRaises(ConfigurationError):
            load_provider_settings({"ELMAN_AI_TIMEOUT_SECONDS": "601"})

    def test_remote_base_url_requires_https(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_provider_settings(
                {"ELMAN_AI_BASE_URL": "http://provider.example/v1"}
            )

    def test_local_http_base_url_is_allowed(self) -> None:
        settings = load_provider_settings(
            {"ELMAN_AI_BASE_URL": "http://127.0.0.1:11434/v1/"}
        )

        self.assertEqual(settings.base_url, "http://127.0.0.1:11434/v1")

    def test_base_url_rejects_embedded_credentials(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_provider_settings(
                {"ELMAN_AI_BASE_URL": "https://user:password@provider.example"}
            )

    def test_runtime_policy_defaults_are_bounded(self) -> None:
        settings = load_provider_settings({})

        self.assertEqual(settings.retry_policy.max_attempts, 3)
        self.assertEqual(settings.usage_budget.max_provider_calls, 10)
        self.assertEqual(settings.usage_budget.max_total_tokens, 100_000)
        self.assertEqual(settings.usage_budget.max_elapsed_seconds, 300)

    def test_runtime_policy_is_loaded_from_environment(self) -> None:
        settings = load_provider_settings(
            {
                "ELMAN_AI_MAX_ATTEMPTS": "4",
                "ELMAN_AI_RETRY_INITIAL_SECONDS": "0.5",
                "ELMAN_AI_RETRY_MAX_SECONDS": "8",
                "ELMAN_AI_RETRY_MULTIPLIER": "3",
                "ELMAN_AI_BUDGET_MAX_CALLS": "20",
                "ELMAN_AI_BUDGET_MAX_TOKENS": "250000",
                "ELMAN_AI_BUDGET_MAX_SECONDS": "900",
            }
        )

        self.assertEqual(settings.retry_policy.max_attempts, 4)
        self.assertEqual(settings.retry_policy.initial_delay_seconds, 0.5)
        self.assertEqual(settings.retry_policy.max_delay_seconds, 8)
        self.assertEqual(settings.retry_policy.backoff_multiplier, 3)
        self.assertEqual(settings.usage_budget.max_provider_calls, 20)
        self.assertEqual(settings.usage_budget.max_total_tokens, 250_000)
        self.assertEqual(settings.usage_budget.max_elapsed_seconds, 900)

    def test_invalid_runtime_limits_fail_without_echoing_value(self) -> None:
        invalid_value = "secret-looking-retry-count"

        with self.assertRaises(ConfigurationError) as raised:
            load_provider_settings({"ELMAN_AI_MAX_ATTEMPTS": invalid_value})

        self.assertNotIn(invalid_value, str(raised.exception))

    def test_retry_delay_order_is_validated(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_provider_settings(
                {
                    "ELMAN_AI_RETRY_INITIAL_SECONDS": "10",
                    "ELMAN_AI_RETRY_MAX_SECONDS": "1",
                }
            )


if __name__ == "__main__":
    unittest.main()
