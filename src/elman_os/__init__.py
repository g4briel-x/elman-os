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
from .planning import PipelinePlanner, ProjectIntent, ProjectKind
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

__version__ = "0.4.0"
