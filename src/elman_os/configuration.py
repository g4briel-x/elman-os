"""Secure, provider-neutral configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

from .execution import RetryPolicy, UsageBudget


PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
AUTH_MODES = frozenset({"api_key", "none"})


class ConfigurationError(ValueError):
    """Configuration failure whose message never contains a secret value."""


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """Secret wrapper requiring an explicit operation to reveal its value."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value.strip():
            raise ValueError("Un secret ne peut pas être vide")

    def reveal(self) -> str:
        """Return the secret for a provider adapter at the network boundary."""

        return self._value

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "SecretValue('***REDACTED***')"

    def __str__(self) -> str:
        return "***REDACTED***"


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated settings shared by future provider-specific adapters."""

    provider_id: str
    model: str
    auth_mode: str
    api_key: SecretValue | None = field(default=None, repr=False)
    base_url: str | None = None
    timeout_seconds: float = 60.0
    max_output_tokens: int = 2_048
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    usage_budget: UsageBudget = field(default_factory=UsageBudget)

    def __post_init__(self) -> None:
        if not PROVIDER_ID_PATTERN.fullmatch(self.provider_id):
            raise ConfigurationError("ELMAN_AI_PROVIDER contient un identifiant invalide")
        if not MODEL_ID_PATTERN.fullmatch(self.model):
            raise ConfigurationError("ELMAN_AI_MODEL contient un identifiant invalide")
        if self.auth_mode not in AUTH_MODES:
            raise ConfigurationError(
                "ELMAN_AI_AUTH_MODE doit valoir 'api_key' ou 'none'"
            )
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ConfigurationError(
                "ELMAN_AI_API_KEY est requise lorsque l'authentification est 'api_key'"
            )
        if self.auth_mode == "none" and self.api_key is not None:
            raise ConfigurationError(
                "ELMAN_AI_API_KEY ne doit pas être définie lorsque "
                "l'authentification est 'none'"
            )
        if not 0.0 < self.timeout_seconds <= 600.0:
            raise ConfigurationError(
                "ELMAN_AI_TIMEOUT_SECONDS doit être compris entre 0 et 600"
            )
        if not 1 <= self.max_output_tokens <= 1_000_000:
            raise ConfigurationError(
                "ELMAN_AI_MAX_OUTPUT_TOKENS doit être compris entre 1 et 1 000 000"
            )
        if self.base_url is not None:
            _validate_base_url(self.base_url)

    def safe_summary(self) -> dict[str, object]:
        """Return diagnostics that are safe to serialize or log."""

        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "auth_mode": self.auth_mode,
            "credential_configured": self.api_key is not None,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "retry_policy": {
                "max_attempts": self.retry_policy.max_attempts,
                "initial_delay_seconds": self.retry_policy.initial_delay_seconds,
                "max_delay_seconds": self.retry_policy.max_delay_seconds,
                "backoff_multiplier": self.retry_policy.backoff_multiplier,
            },
            "usage_budget": {
                "max_provider_calls": self.usage_budget.max_provider_calls,
                "max_total_tokens": self.usage_budget.max_total_tokens,
                "max_elapsed_seconds": self.usage_budget.max_elapsed_seconds,
            },
        }


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        raise ConfigurationError("ELMAN_AI_BASE_URL doit être une URL absolue")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(
            "ELMAN_AI_BASE_URL ne doit pas contenir d'identifiants"
        )
    is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ConfigurationError(
            "ELMAN_AI_BASE_URL doit utiliser HTTPS, sauf pour une adresse locale"
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "ELMAN_AI_BASE_URL ne doit contenir ni paramètres ni fragment"
        )


def _read_number(
    environment: Mapping[str, str],
    name: str,
    default: str,
    converter: type[int] | type[float],
) -> int | float:
    raw_value = environment.get(name, default).strip()
    try:
        return converter(raw_value)
    except ValueError:
        raise ConfigurationError(
            f"{name} doit être une valeur numérique valide"
        ) from None


def load_provider_settings(
    environment: Mapping[str, str] | None = None,
) -> ProviderSettings:
    """Load validated settings without reading files or contacting a provider."""

    source = os.environ if environment is None else environment
    provider_id = source.get("ELMAN_AI_PROVIDER", "deterministic-model").strip()
    model = source.get("ELMAN_AI_MODEL", "deterministic-v1").strip()
    default_auth_mode = "none" if provider_id == "deterministic-model" else "api_key"
    auth_mode = source.get("ELMAN_AI_AUTH_MODE", default_auth_mode).strip().lower()
    raw_api_key = source.get("ELMAN_AI_API_KEY")
    if raw_api_key is not None and not raw_api_key.strip():
        raise ConfigurationError(
            "ELMAN_AI_API_KEY ne peut pas être vide lorsqu'elle est définie"
        )
    api_key = SecretValue(raw_api_key) if raw_api_key is not None else None
    raw_base_url = source.get("ELMAN_AI_BASE_URL")
    base_url = raw_base_url.strip().rstrip("/") if raw_base_url else None

    try:
        retry_policy = RetryPolicy(
            max_attempts=int(
                _read_number(source, "ELMAN_AI_MAX_ATTEMPTS", "3", int)
            ),
            initial_delay_seconds=float(
                _read_number(
                    source,
                    "ELMAN_AI_RETRY_INITIAL_SECONDS",
                    "0.25",
                    float,
                )
            ),
            max_delay_seconds=float(
                _read_number(
                    source,
                    "ELMAN_AI_RETRY_MAX_SECONDS",
                    "5",
                    float,
                )
            ),
            backoff_multiplier=float(
                _read_number(
                    source,
                    "ELMAN_AI_RETRY_MULTIPLIER",
                    "2",
                    float,
                )
            ),
        )
        usage_budget = UsageBudget(
            max_provider_calls=int(
                _read_number(
                    source,
                    "ELMAN_AI_BUDGET_MAX_CALLS",
                    "10",
                    int,
                )
            ),
            max_total_tokens=int(
                _read_number(
                    source,
                    "ELMAN_AI_BUDGET_MAX_TOKENS",
                    "100000",
                    int,
                )
            ),
            max_elapsed_seconds=float(
                _read_number(
                    source,
                    "ELMAN_AI_BUDGET_MAX_SECONDS",
                    "300",
                    float,
                )
            ),
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from None

    return ProviderSettings(
        provider_id=provider_id,
        model=model,
        auth_mode=auth_mode,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=float(
            _read_number(
                source,
                "ELMAN_AI_TIMEOUT_SECONDS",
                "60",
                float,
            )
        ),
        max_output_tokens=int(
            _read_number(
                source,
                "ELMAN_AI_MAX_OUTPUT_TOKENS",
                "2048",
                int,
            )
        ),
        retry_policy=retry_policy,
        usage_budget=usage_budget,
    )
