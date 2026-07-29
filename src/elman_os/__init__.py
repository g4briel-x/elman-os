"""ELMAN-OS multi-agent governance and generation kernel."""

from .catalog import AGENT_CATALOG, get_agent
from .domain import StopReason, Verdict, WorkflowStatus
from .metacognition import SupervisorPolicy
from .planning import PipelinePlanner, ProjectIntent, ProjectKind
from .service import ElmanKernelService
from .workflow import ElmanWorkflow

__all__ = [
    "AGENT_CATALOG",
    "ElmanKernelService",
    "ElmanWorkflow",
    "PipelinePlanner",
    "ProjectIntent",
    "ProjectKind",
    "StopReason",
    "SupervisorPolicy",
    "Verdict",
    "WorkflowStatus",
    "get_agent",
]

__version__ = "0.3.1"
