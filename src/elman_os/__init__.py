"""ELMAN-OS multi-agent governance and generation kernel."""

from .catalog import AGENT_CATALOG, get_agent
from .configuration import ProviderSettings, load_provider_settings
from .domain import StopReason, Verdict, WorkflowStatus
from .execution import ResilientAIExecutor, RetryPolicy, UsageBudget
from .metacognition import SupervisorPolicy
from .planning import PipelinePlanner, ProjectIntent, ProjectKind
from .provider import AIProvider, ModelRequest, ModelResponse
from .service import ElmanKernelService
from .workflow import ElmanWorkflow

__all__ = [
    "AGENT_CATALOG",
    "AIProvider",
    "ElmanKernelService",
    "ElmanWorkflow",
    "ModelRequest",
    "ModelResponse",
    "PipelinePlanner",
    "ProviderSettings",
    "ProjectIntent",
    "ProjectKind",
    "ResilientAIExecutor",
    "RetryPolicy",
    "StopReason",
    "SupervisorPolicy",
    "UsageBudget",
    "Verdict",
    "WorkflowStatus",
    "get_agent",
    "load_provider_settings",
]

__version__ = "0.4.0a3"
