"""Production composition for authenticated, tenant-persistent AI execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import (
    AuditIntegrityError,
    AuditSigner,
    AuditedAIExecutor,
    ExecutionAuditContext,
    ExecutionAuthorizationError,
    ExecutionAuthorizationPolicy,
    ExecutionPurpose,
)
from .authentication import JwtOidcAuthenticator, TokenAuthenticationError
from .configuration import ProviderSettings
from .execution import ExecutionResult, ResilientAIExecutor
from .governance import (
    ConfigurationCompatibilityError,
    IdentityQuota,
    IdentityQuotaExceededError,
    StabilizedAIExecutor,
    check_configuration_compatibility,
)
from .persistent_governance import (
    PersistentAuditTrail,
    PersistentIdentityQuotaManager,
)
from .provider import (
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
)
from .registry import ConfiguredAIRuntime, ProviderRegistry
from .transactional_persistence import PersistenceBackend, SQLitePersistence


@dataclass(slots=True)
class PersistentGovernedAIExecutor:
    """Apply authorization, tenant quotas and persistent audit to every request."""

    executor: ResilientAIExecutor
    backend: PersistenceBackend
    signer: AuditSigner
    selected_model: str
    quota: IdentityQuota = field(default_factory=IdentityQuota)
    policy: ExecutionAuthorizationPolicy = field(
        default_factory=ExecutionAuthorizationPolicy
    )
    reservation_ttl_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.selected_model.strip():
            raise ValueError("selected_model est obligatoire")
        if not 0.0 < self.reservation_ttl_seconds <= 86_400.0:
            raise ValueError(
                "reservation_ttl_seconds doit être compris entre 0 et 86 400"
            )

    def audit_trail(self, tenant_id: str) -> PersistentAuditTrail:
        """Return the persistent audit view for one tenant."""

        return PersistentAuditTrail(self.signer, self.backend, tenant_id)

    def quota_manager(self, tenant_id: str) -> PersistentIdentityQuotaManager:
        """Return the persistent quota view for one tenant."""

        return PersistentIdentityQuotaManager(
            tenant_id,
            self.backend,
            quota=self.quota,
            reservation_ttl_seconds=self.reservation_ttl_seconds,
        )

    async def generate(
        self,
        request: ModelRequest,
        context: ExecutionAuditContext,
    ) -> ExecutionResult:
        """Execute through the complete fail-closed production boundary."""

        tenant_id = context.principal.tenant_id
        audited = AuditedAIExecutor(
            self.executor,
            self.audit_trail(tenant_id),
            policy=self.policy,
        )
        stabilized = StabilizedAIExecutor(
            audited,
            self.quota_manager(tenant_id),
            self.selected_model,
        )
        return await stabilized.generate(request, context)


@dataclass(slots=True)
class AuthenticatedExecutionService:
    """Authenticate a bearer token before entering the governed runtime."""

    authenticator: JwtOidcAuthenticator
    executor: PersistentGovernedAIExecutor

    async def generate(
        self,
        token: str,
        request: ModelRequest,
        *,
        purpose: ExecutionPurpose = ExecutionPurpose.AGENT_TASK,
        correlation_id: str | None = None,
    ) -> ExecutionResult:
        principal = self.authenticator.authenticate(token)
        context = ExecutionAuditContext(
            principal=principal,
            purpose=purpose,
            correlation_id=correlation_id or str(uuid.uuid4()),
        )
        return await self.executor.generate(request, context)


@dataclass(slots=True)
class ProductionAIRuntime:
    """Owned production resources and their composed execution service."""

    configured: ConfiguredAIRuntime
    backend: PersistenceBackend
    executor: PersistentGovernedAIExecutor
    owns_backend: bool = False
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_settings(
        cls,
        registry: ProviderRegistry,
        settings: ProviderSettings,
        *,
        backend: PersistenceBackend,
        signer: AuditSigner,
        quota: IdentityQuota | None = None,
        reservation_ttl_seconds: float = 300.0,
        required_capabilities: frozenset[ModelCapability] = frozenset(
            {ModelCapability.TEXT_GENERATION}
        ),
        owns_backend: bool = False,
    ) -> "ProductionAIRuntime":
        report = check_configuration_compatibility(
            settings,
            registry,
            required_capabilities=required_capabilities,
        )
        if not report.valid:
            raise ConfigurationCompatibilityError(report)
        configured = ConfiguredAIRuntime.from_settings(
            registry,
            settings,
            required_capabilities=required_capabilities,
        )
        executor = PersistentGovernedAIExecutor(
            configured.executor,
            backend,
            signer,
            configured.selection.selected_model,
            quota=quota or IdentityQuota(),
            reservation_ttl_seconds=reservation_ttl_seconds,
        )
        return cls(configured, backend, executor, owns_backend=owns_backend)

    @classmethod
    def from_sqlite(
        cls,
        registry: ProviderRegistry,
        settings: ProviderSettings,
        *,
        database_path: str | Path,
        signer: AuditSigner,
        quota: IdentityQuota | None = None,
        reservation_ttl_seconds: float = 300.0,
        required_capabilities: frozenset[ModelCapability] = frozenset(
            {ModelCapability.TEXT_GENERATION}
        ),
    ) -> "ProductionAIRuntime":
        backend = SQLitePersistence(Path(database_path))
        try:
            return cls.from_settings(
                registry,
                settings,
                backend=backend,
                signer=signer,
                quota=quota,
                reservation_ttl_seconds=reservation_ttl_seconds,
                required_capabilities=required_capabilities,
                owns_backend=True,
            )
        except Exception:
            # Construction has not entered an event loop yet. SQLitePersistence
            # opens lazily, so no asynchronous close is required on this path.
            raise

    def authenticated(
        self,
        authenticator: JwtOidcAuthenticator,
    ) -> AuthenticatedExecutionService:
        return AuthenticatedExecutionService(authenticator, self.executor)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.configured.close()
        finally:
            if self.owns_backend:
                await self.backend.close()

    async def __aenter__(self) -> "ProductionAIRuntime":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def attach_execution_routes(
    app: Any,
    service: AuthenticatedExecutionService,
) -> None:
    """Attach the authenticated generation endpoint to a FastAPI application."""

    try:
        from fastapi import Header, HTTPException
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError(
            'Installer le control plane avec: python -m pip install -e ".[api]"'
        ) from exc

    @app.post("/v1/ai/generate")
    async def generate_ai(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="bearer_token_required")
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="bearer_token_required")

        try:
            raw_messages = payload["messages"]
            if not isinstance(raw_messages, list) or not raw_messages:
                raise ValueError
            messages = tuple(
                ModelMessage(
                    MessageRole(item["role"]),
                    str(item["content"]),
                    (
                        str(item["name"])
                        if item.get("name") is not None
                        else None
                    ),
                )
                for item in raw_messages
                if isinstance(item, dict)
            )
            if len(messages) != len(raw_messages):
                raise ValueError
            request = ModelRequest(
                request_id=str(payload.get("request_id") or uuid.uuid4()),
                model=str(payload.get("model") or service.executor.selected_model),
                messages=messages,
                max_output_tokens=int(payload.get("max_output_tokens", 2048)),
                temperature=float(payload.get("temperature", 0.2)),
                timeout_seconds=float(payload.get("timeout_seconds", 60.0)),
            )
            purpose = ExecutionPurpose(
                payload.get("purpose", ExecutionPurpose.AGENT_TASK.value)
            )
            correlation_id = payload.get("correlation_id")
            result = await service.generate(
                token,
                request,
                purpose=purpose,
                correlation_id=(
                    str(correlation_id) if correlation_id is not None else None
                ),
            )
        except TokenAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=exc.code.value) from exc
        except ExecutionAuthorizationError as exc:
            raise HTTPException(status_code=403, detail="execution_denied") from exc
        except IdentityQuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=exc.code.value) from exc
        except AuditIntegrityError as exc:
            raise HTTPException(status_code=503, detail="audit_unavailable") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid_request") from exc

        response = result.response
        return {
            "request_id": response.request_id,
            "provider_id": response.provider_id,
            "model": response.model,
            "content": response.content,
            "finish_reason": response.finish_reason.value,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "attempts": result.attempts,
        }
