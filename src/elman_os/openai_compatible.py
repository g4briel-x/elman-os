"""OpenAI-compatible chat-completions adapter with an injectable transport.

The adapter owns protocol translation and error normalization.  Network I/O is
delegated to ``AsyncHTTPTransport`` so tests can remain fully offline.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from .configuration import ProviderSettings, SecretValue
from .provider import (
    FinishReason,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ProviderDescriptor,
    ProviderError,
    ProviderErrorCode,
    TokenUsage,
)


OPENAI_PROVIDER_ID = "openai"
OPENAI_COMPATIBLE_PROVIDER_ID = "openai-compatible"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True, slots=True)
class HTTPRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class HTTPTransportError(RuntimeError):
    """Transport failure without request bodies, headers or credentials."""


@runtime_checkable
class AsyncHTTPTransport(Protocol):
    async def send(self, request: HTTPRequest) -> HTTPResponse:
        """Send one HTTP request."""

    async def close(self) -> None:
        """Release transport resources."""


@dataclass(slots=True)
class UrllibAsyncTransport:
    """Small standard-library transport used only when explicitly executed."""

    closed: bool = field(default=False, init=False)

    async def send(self, request: HTTPRequest) -> HTTPResponse:
        if self.closed:
            raise HTTPTransportError("Le transport HTTP est fermé")
        return await asyncio.to_thread(self._send_sync, request)

    @staticmethod
    def _send_sync(request: HTTPRequest) -> HTTPResponse:
        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(
                raw_request,
                timeout=request.timeout_seconds,
            ) as response:
                return HTTPResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HTTPResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise HTTPTransportError(
                "Échec du transport HTTP vers le fournisseur IA"
            ) from exc

    async def close(self) -> None:
        self.closed = True


def openai_compatible_descriptor(provider_id: str) -> ProviderDescriptor:
    if provider_id not in {OPENAI_PROVIDER_ID, OPENAI_COMPATIBLE_PROVIDER_ID}:
        raise ValueError("provider_id OpenAI-compatible invalide")
    display_name = (
        "OpenAI"
        if provider_id == OPENAI_PROVIDER_ID
        else "OpenAI-compatible API"
    )
    return ProviderDescriptor(
        provider_id=provider_id,
        display_name=display_name,
        capabilities=frozenset({ModelCapability.TEXT_GENERATION}),
        models=(),
    )


@dataclass(slots=True)
class OpenAICompatibleProvider:
    """Translate the stable ELMAN contract to ``/chat/completions``."""

    provider_id: str
    api_key: SecretValue = field(repr=False)
    base_url: str
    transport: AsyncHTTPTransport = field(
        default_factory=UrllibAsyncTransport,
        repr=False,
    )
    closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        openai_compatible_descriptor(self.provider_id)
        if not self.base_url.strip():
            raise ValueError("base_url ne peut pas être vide")
        self.base_url = self.base_url.rstrip("/")
        if not isinstance(self.transport, AsyncHTTPTransport):
            raise TypeError("transport doit respecter AsyncHTTPTransport")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return openai_compatible_descriptor(self.provider_id)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self.closed:
            raise ProviderError(
                ProviderErrorCode.SERVICE_UNAVAILABLE,
                "Le fournisseur est fermé",
                provider_id=self.provider_id,
            )
        missing = request.required_capabilities - self.descriptor.capabilities
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"Capacités non prises en charge : {names}",
                provider_id=self.provider_id,
            )

        payload = {
            "model": request.model,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    **({"name": message.name} if message.name is not None else {}),
                }
                for message in request.messages
            ],
            "temperature": request.temperature,
        }
        token_parameter = (
            "max_completion_tokens"
            if self.provider_id == OPENAI_PROVIDER_ID
            else "max_tokens"
        )
        payload[token_parameter] = request.max_output_tokens
        http_request = HTTPRequest(
            method="POST",
            url=f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key.reveal()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ELMAN-OS/0.4",
                "X-Request-ID": request.request_id,
            },
            body=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            timeout_seconds=request.timeout_seconds,
        )
        try:
            response = await self.transport.send(http_request)
        except HTTPTransportError as exc:
            raise ProviderError(
                ProviderErrorCode.NETWORK,
                "Le transport vers le fournisseur IA a échoué",
                provider_id=self.provider_id,
                retryable=True,
            ) from None

        if not 200 <= response.status_code < 300:
            raise self._http_error(response)
        return self._decode_response(request, response)

    def _http_error(self, response: HTTPResponse) -> ProviderError:
        status = response.status_code
        mapping = {
            400: (ProviderErrorCode.INVALID_REQUEST, False),
            401: (ProviderErrorCode.AUTHENTICATION, False),
            403: (ProviderErrorCode.AUTHORIZATION, False),
            404: (ProviderErrorCode.MODEL_NOT_FOUND, False),
            408: (ProviderErrorCode.TIMEOUT, True),
            409: (ProviderErrorCode.INVALID_REQUEST, False),
            422: (ProviderErrorCode.INVALID_REQUEST, False),
            429: (ProviderErrorCode.RATE_LIMITED, True),
        }
        code, retryable = mapping.get(
            status,
            (
                ProviderErrorCode.SERVICE_UNAVAILABLE
                if 500 <= status <= 599
                else ProviderErrorCode.UNKNOWN,
                500 <= status <= 599,
            ),
        )
        retry_after = _retry_after_seconds(response.headers) if retryable else None
        return ProviderError(
            code,
            f"Le fournisseur IA a répondu avec le statut HTTP {status}",
            provider_id=self.provider_id,
            retryable=retryable,
            retry_after_seconds=retry_after,
        )

    def _decode_response(
        self,
        request: ModelRequest,
        response: HTTPResponse,
    ) -> ModelResponse:
        try:
            payload = json.loads(response.body.decode("utf-8"))
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            usage = payload.get("usage", {})
            provider_request_id = payload.get("id")
            response_model = payload.get("model", request.model)
            finish_reason = _finish_reason(choice.get("finish_reason"))
            if not isinstance(content, str):
                raise ValueError
            if not content and finish_reason != FinishReason.CONTENT_FILTER:
                raise ValueError
            if not isinstance(response_model, str) or not response_model:
                raise ValueError
            if provider_request_id is not None and not isinstance(
                provider_request_id,
                str,
            ):
                raise ValueError
            input_tokens = _non_negative_int(usage.get("prompt_tokens", 0))
            output_tokens = _non_negative_int(usage.get("completion_tokens", 0))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Le fournisseur IA a renvoyé une réponse JSON invalide",
                provider_id=self.provider_id,
            ) from exc

        return ModelResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=response_model,
            content=content,
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            provider_request_id=provider_request_id,
        )

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.transport.close()


def provider_from_settings(
    settings: ProviderSettings,
    *,
    transport: AsyncHTTPTransport | None = None,
) -> OpenAICompatibleProvider:
    """Build an adapter without contacting its endpoint."""

    if settings.provider_id not in {
        OPENAI_PROVIDER_ID,
        OPENAI_COMPATIBLE_PROVIDER_ID,
    }:
        raise ValueError("Configuration destinée à un autre fournisseur")
    if settings.api_key is None or settings.auth_mode != "api_key":
        raise ValueError("Une clé API est requise pour cet adaptateur")
    if settings.provider_id == OPENAI_PROVIDER_ID:
        base_url = settings.base_url or OPENAI_DEFAULT_BASE_URL
    elif settings.base_url is None:
        raise ValueError(
            "ELMAN_AI_BASE_URL est requise pour openai-compatible"
        )
    else:
        base_url = settings.base_url
    return OpenAICompatibleProvider(
        provider_id=settings.provider_id,
        api_key=settings.api_key,
        base_url=base_url,
        transport=transport or UrllibAsyncTransport(),
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw_value = next(
        (
            value
            for name, value in headers.items()
            if name.lower() == "retry-after"
        ),
        None,
    )
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except ValueError:
        return None
    return value if 0.0 <= value <= 300.0 else None


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _finish_reason(value: object) -> FinishReason:
    mapping = {
        "stop": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "tool_calls": FinishReason.TOOL_CALL,
        "content_filter": FinishReason.CONTENT_FILTER,
    }
    return mapping.get(value, FinishReason.UNKNOWN)
