"""Typed contracts shared by ELMAN-OS agents and supervisors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class AgentLayer(StrEnum):
    ORCHESTRATION = "orchestration"
    PRODUCTION = "production"
    VERIFICATION = "verification"
    METACOGNITION = "metacognition"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Verdict(StrEnum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    BLOCKED = "BLOCKED"


class WorkflowStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    READY_FOR_HUMAN_APPROVAL = "ready_for_human_approval"
    STOPPED_LIMIT = "stopped_limit"
    BLOCKED = "blocked"
    FAILED = "failed"


class StopReason(StrEnum):
    CONTINUE = "continue"
    CRITERIA_VALIDATED = "criteria_validated"
    MAX_ITERATIONS = "max_iterations"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIME_LIMIT = "time_limit"
    CRITICAL_FINDING = "critical_finding"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    REPEATED_FAILURE = "repeated_failure"
    NO_PROGRESS = "no_progress"
    EXTERNAL_BLOCKER = "external_blocker"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    name: str
    layer: AgentLayer
    role: str
    mission: str
    required_outputs: tuple[str, ...]
    allowed_scopes: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    experience_standard: str = "15-plus-years-equivalent"


@dataclass(slots=True)
class TaskEnvelope:
    task_id: str
    project_id: str
    owner_agent: str
    objective: str
    inputs: list[str]
    expected_outputs: list[str]
    acceptance_criteria: list[str]
    constraints: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    parent_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk_level"] = self.risk_level.value
        return data


@dataclass(frozen=True, slots=True)
class Evidence:
    claim: str
    source: str
    observed: bool = True


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    severity: RiskLevel
    summary: str
    evidence: str
    owner_agent: str
    requires_human_decision: bool = False


@dataclass(slots=True)
class AgentOutput:
    agent_id: str
    task_id: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    confidence: str = "medium"


@dataclass(slots=True)
class CycleResult:
    proof_verdict: Verdict
    criteria_validated: bool
    progress_score: float
    cost_units: float
    evidence: list[str] = field(default_factory=list)
    failure_fingerprint: str | None = None
    critical_findings: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReflectionReport:
    iteration: int
    what_worked: tuple[str, ...]
    what_failed: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    recommended_correction: str | None
    failure_fingerprint: str | None
    probable_causes: tuple[str, ...] = ()
    hypotheses_to_verify: tuple[str, ...] = ()
    proposed_improvements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    should_continue: bool
    reason: StopReason
    message: str
    requires_human_decision: bool = False


@dataclass(frozen=True, slots=True)
class LearningProposal:
    proposal_id: str
    pattern: str
    evidence: tuple[str, ...]
    confidence: str
    approved: bool = False


@dataclass(slots=True)
class IterationRecord:
    iteration: int
    result: CycleResult
    reflection: ReflectionReport
    decision: SupervisorDecision


@dataclass(slots=True)
class WorkflowReport:
    workflow_id: str
    status: WorkflowStatus
    stop_reason: StopReason
    iterations: list[IterationRecord]
    learning_proposals: list[LearningProposal]
    memory_snapshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

