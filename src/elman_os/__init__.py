"""ELMAN-OS multi-agent governance and generation kernel."""

from .catalog import AGENT_CATALOG, get_agent
from .domain import StopReason, Verdict, WorkflowStatus
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
    "ProjectIntent",
    "ProjectKind",
    "StopReason",
    "SupervisorPolicy",
    "Verdict",
    "WorkflowStatus",
    "get_agent",
]

__version__ = "0.4.0a1"
