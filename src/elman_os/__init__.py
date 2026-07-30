"""ELMAN-OS multi-agent governance and generation kernel."""

from .catalog import AGENT_CATALOG, get_agent
from .audit import (
    AuditedAIExecutor,
    AuditSigner,
    AuditTrail,
    ExecutionAuditContext,
    ExecutionPrincipal,
    FileAuditSink,
)
from .authentication import (
    AuthenticationErrorCode,
    HmacSha256Verifier,
    JwtOidcAuthenticator,
    SignatureVerifier,
    TokenAuthenticationError,
    TokenValidationPolicy,
)
from .configuration import ProviderSettings, load_provider_settings
from .domain import StopReason, Verdict, WorkflowStatus
from .execution import ResilientAIExecutor, RetryPolicy, UsageBudget
from .governance import (
    IdentityQuota,
    IdentityQuotaManager,
    StabilizedAIExecutor,
    StabilizedAIRuntime,
    check_configuration_compatibility,
)
from .metacognition import SupervisorPolicy
from .openai_compatible import OpenAICompatibleProvider
from .persistent_governance import (
    PersistentAuditTrail,
    PersistentIdentityQuotaManager,
)
from .planning import PipelinePlanner, ProjectIntent, ProjectKind
from .production_runtime import (
    AuthenticatedExecutionService,
    PersistentGovernedAIExecutor,
    ProductionAIRuntime,
    attach_execution_routes,
)
from .provider import AIProvider, ModelRequest, ModelResponse
from .registry import (
    ConfiguredAIRuntime,
    ProviderRegistry,
    built_in_provider_registry,
)
from .release import ReleaseReport, validate_release
from .service import ElmanKernelService
from .workflow import ElmanWorkflow

__all__ = [
    "AuthenticatedExecutionService",
    "PersistentGovernedAIExecutor",
    "ProductionAIRuntime",
    "attach_execution_routes",
    "PersistentIdentityQuotaManager",
    "PersistentAuditTrail",
    "PersistenceBackend",
    "PersistenceConflictError",
    "PersistenceError",
    "PersistenceIntegrityError",
    "PersistenceTransaction",
    "SQLitePersistence",
    "StoredRecord",
    "TransactionClosedError",
    "AGENT_CATALOG",
    "AIProvider",
    "AuthenticationErrorCode",
    "HmacSha256Verifier",
    "JwtOidcAuthenticator",
    "SignatureVerifier",
    "TokenAuthenticationError",
    "TokenValidationPolicy",
    "AuditedAIExecutor",
    "AuditSigner",
    "AuditTrail",
    "ConfiguredAIRuntime",
    "ElmanKernelService",
    "ElmanWorkflow",
    "ExecutionAuditContext",
    "ExecutionPrincipal",
    "FileAuditSink",
    "IdentityQuota",
    "IdentityQuotaManager",
    "ModelRequest",
    "ModelResponse",
    "OpenAICompatibleProvider",
    "PipelinePlanner",
    "ProviderSettings",
    "ProviderRegistry",
    "ProjectIntent",
    "ProjectKind",
    "ReleaseReport",
    "ResilientAIExecutor",
    "RetryPolicy",
    "StopReason",
    "StabilizedAIExecutor",
    "StabilizedAIRuntime",
    "SupervisorPolicy",
    "UsageBudget",
    "Verdict",
    "WorkflowStatus",
    "get_agent",
    "built_in_provider_registry",
    "check_configuration_compatibility",
    "load_provider_settings",
    "validate_release",
]

__version__ = "0.5.0"

# Transactional persistence boundary (additive to SQLiteKernelStore).
from .transactional_persistence import (
    PersistenceBackend,
    PersistenceConflictError,
    PersistenceError,
    PersistenceIntegrityError,
    PersistenceTransaction,
    SQLitePersistence,
    StoredRecord,
    TransactionClosedError,
)
