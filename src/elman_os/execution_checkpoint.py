"""Verifiable execution checkpoints and resume assessment for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .agent_contracts import canonical_json
from .execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
    JournalIntegrityError,
)
from .execution_plan import (
    ExecutionPlan,
    PlanStatus,
    StepStatus,
)


CHECKPOINT_FORMAT_VERSION = 1

_CHECKPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionCheckpointError(ValueError):
    """A checkpoint is malformed or incompatible with plan/journal state."""


class CheckpointIntegrityError(ExecutionCheckpointError):
    """The checkpoint hash or referenced integrity data is invalid."""


class PlanJournalCompatibilityError(ExecutionCheckpointError):
    """The execution plan and journal do not represent the same state."""


class CheckpointStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class ResumeAssessmentStatus(StrEnum):
    READY = "ready"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


_PLAN_EVENT_STATUS: dict[ExecutionEventType, PlanStatus] = {
    ExecutionEventType.PLAN_CREATED: PlanStatus.PENDING,
    ExecutionEventType.PLAN_APPROVED: PlanStatus.APPROVED,
    ExecutionEventType.PLAN_STARTED: PlanStatus.RUNNING,
    ExecutionEventType.PLAN_BLOCKED: PlanStatus.BLOCKED,
    ExecutionEventType.PLAN_FAILED: PlanStatus.FAILED,
    ExecutionEventType.PLAN_COMPLETED: PlanStatus.COMPLETED,
}

_STEP_EVENT_STATUS: dict[ExecutionEventType, StepStatus] = {
    ExecutionEventType.STEP_READY: StepStatus.PENDING,
    ExecutionEventType.STEP_APPROVED: StepStatus.APPROVED,
    ExecutionEventType.STEP_STARTED: StepStatus.RUNNING,
    ExecutionEventType.STEP_BLOCKED: StepStatus.BLOCKED,
    ExecutionEventType.STEP_FAILED: StepStatus.FAILED,
    ExecutionEventType.STEP_COMPLETED: StepStatus.COMPLETED,
}

_STEP_AGENT_EVENTS = frozenset(
    {
        ExecutionEventType.STEP_ASSIGNED,
        ExecutionEventType.STEP_STARTED,
        ExecutionEventType.STEP_BLOCKED,
        ExecutionEventType.STEP_FAILED,
        ExecutionEventType.STEP_COMPLETED,
    }
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionCheckpointError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str],
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ExecutionCheckpointError(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ExecutionCheckpointError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _utc_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ExecutionCheckpointError(
                "created_at datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ExecutionCheckpointError(
                "created_at datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ExecutionCheckpointError(
                "created_at must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ExecutionCheckpointError(
                "created_at is not valid ISO-8601 UTC"
            ) from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ExecutionCheckpointError("created_at must be UTC")
    else:
        raise ExecutionCheckpointError(
            "created_at must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _definition_document(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "objective": plan.objective,
        "created_by": plan.created_by,
        "requires_human_approval": plan.requires_human_approval,
        "metadata": plan.to_dict()["metadata"],
        "steps": [
            {
                "step_id": step.step_id,
                "title": step.title,
                "capability_id": step.capability_id,
                "objective": step.objective,
                "dependencies": list(step.dependencies),
                "required_permissions": list(step.required_permissions),
                "requires_human_approval": step.requires_human_approval,
                "metadata": step.to_dict()["metadata"],
            }
            for step in plan.steps
        ],
    }


def _plan_definition_hash(plan: ExecutionPlan) -> str:
    return _sha256(_definition_document(plan))


def _plan_state_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StepCheckpointState:
    step_id: str
    status: StepStatus
    assigned_agent_id: str | None
    approval_reference: str | None

    def __post_init__(self) -> None:
        _text(self.step_id, "step_id")
        try:
            status = StepStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ExecutionCheckpointError(
                "step checkpoint status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        if self.assigned_agent_id is not None:
            object.__setattr__(
                self,
                "assigned_agent_id",
                _text(self.assigned_agent_id, "assigned_agent_id"),
            )
        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _text(self.approval_reference, "approval_reference"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "assigned_agent_id": self.assigned_agent_id,
            "approval_reference": self.approval_reference,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "StepCheckpointState":
        return cls(
            step_id=data["step_id"],
            status=StepStatus(data["status"]),
            assigned_agent_id=data.get("assigned_agent_id"),
            approval_reference=data.get("approval_reference"),
        )


@dataclass(frozen=True, slots=True)
class ResumeAssessment:
    checkpoint_id: str
    status: ResumeAssessmentStatus
    can_resume: bool
    reasons: tuple[str, ...]
    ready_step_ids: tuple[str, ...]
    running_step_ids: tuple[str, ...]
    current_event_count: int
    current_head_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpoint_id",
            _identifier(
                self.checkpoint_id,
                "checkpoint_id",
                _CHECKPOINT_ID,
            ),
        )
        try:
            status = ResumeAssessmentStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ExecutionCheckpointError(
                "resume assessment status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        if not isinstance(self.can_resume, bool):
            raise ExecutionCheckpointError("can_resume must be boolean")

        normalized_reasons = tuple(
            _text(reason, "reason")
            for reason in self.reasons
        )
        object.__setattr__(self, "reasons", normalized_reasons)
        object.__setattr__(
            self,
            "ready_step_ids",
            tuple(sorted({_text(item, "ready_step_id") for item in self.ready_step_ids})),
        )
        object.__setattr__(
            self,
            "running_step_ids",
            tuple(sorted({_text(item, "running_step_id") for item in self.running_step_ids})),
        )

        if (
            isinstance(self.current_event_count, bool)
            or not isinstance(self.current_event_count, int)
            or self.current_event_count < 0
        ):
            raise ExecutionCheckpointError(
                "current_event_count must be non-negative"
            )
        object.__setattr__(
            self,
            "current_head_hash",
            _hash(self.current_head_hash, "current_head_hash"),
        )

        if status is ResumeAssessmentStatus.READY and not self.can_resume:
            raise ExecutionCheckpointError(
                "ready assessment must allow resume"
            )
        if status is not ResumeAssessmentStatus.READY and self.can_resume:
            raise ExecutionCheckpointError(
                "non-ready assessment cannot allow resume"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "status": self.status.value,
            "can_resume": self.can_resume,
            "reasons": list(self.reasons),
            "ready_step_ids": list(self.ready_step_ids),
            "running_step_ids": list(self.running_step_ids),
            "current_event_count": self.current_event_count,
            "current_head_hash": self.current_head_hash,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def _validate_plan_journal_compatibility(
    plan: ExecutionPlan,
    journal: ExecutionJournal,
) -> None:
    if not isinstance(plan, ExecutionPlan):
        raise ExecutionCheckpointError("plan must be an ExecutionPlan")
    if not isinstance(journal, ExecutionJournal):
        raise ExecutionCheckpointError("journal must be an ExecutionJournal")

    if plan.plan_id != journal.plan_id:
        raise PlanJournalCompatibilityError(
            "plan_id does not match journal plan_id"
        )

    journal.validate()

    derived_plan_status = PlanStatus.PENDING
    derived_step_status = {
        step.step_id: StepStatus.PENDING
        for step in plan.steps
    }
    derived_agent = {
        step.step_id: None
        for step in plan.steps
    }
    known_steps = set(derived_step_status)

    for event in journal.events:
        if event.event_type in _PLAN_EVENT_STATUS:
            derived_plan_status = _PLAN_EVENT_STATUS[event.event_type]
            continue

        step_id = event.step_id
        if step_id is None or step_id not in known_steps:
            raise PlanJournalCompatibilityError(
                "journal references an unknown execution step"
            )

        if event.event_type in _STEP_EVENT_STATUS:
            derived_step_status[step_id] = _STEP_EVENT_STATUS[event.event_type]

        if event.event_type in _STEP_AGENT_EVENTS:
            if event.agent_id is None:
                raise PlanJournalCompatibilityError(
                    "journal agent event is missing agent_id"
                )
            derived_agent[step_id] = event.agent_id

    if derived_plan_status is not plan.status:
        raise PlanJournalCompatibilityError(
            "plan status does not match journal-derived status"
        )

    for step in plan.steps:
        if derived_step_status[step.step_id] is not step.status:
            raise PlanJournalCompatibilityError(
                f"{step.step_id} status does not match journal-derived status"
            )
        if derived_agent[step.step_id] != step.assigned_agent_id:
            raise PlanJournalCompatibilityError(
                f"{step.step_id} assigned agent does not match journal"
            )


def _checkpoint_status(plan: ExecutionPlan) -> CheckpointStatus:
    if plan.status in {PlanStatus.FAILED, PlanStatus.COMPLETED}:
        return CheckpointStatus.TERMINAL
    if plan.status is PlanStatus.BLOCKED:
        return CheckpointStatus.BLOCKED
    return CheckpointStatus.READY


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    checkpoint_id: str
    created_at: str
    plan_id: str
    project_id: str
    plan_status: PlanStatus
    checkpoint_status: CheckpointStatus
    step_states: tuple[StepCheckpointState, ...]
    plan_definition_hash: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    checkpoint_hash: str | None = None
    version: int = CHECKPOINT_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpoint_id",
            _identifier(
                self.checkpoint_id,
                "checkpoint_id",
                _CHECKPOINT_ID,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _utc_timestamp(self.created_at),
        )
        object.__setattr__(
            self,
            "plan_id",
            _text(self.plan_id, "plan_id"),
        )
        object.__setattr__(
            self,
            "project_id",
            _text(self.project_id, "project_id"),
        )

        try:
            plan_status = PlanStatus(self.plan_status)
        except (TypeError, ValueError) as exc:
            raise ExecutionCheckpointError(
                "plan_status is invalid"
            ) from exc
        object.__setattr__(self, "plan_status", plan_status)

        try:
            checkpoint_status = CheckpointStatus(self.checkpoint_status)
        except (TypeError, ValueError) as exc:
            raise ExecutionCheckpointError(
                "checkpoint_status is invalid"
            ) from exc
        object.__setattr__(self, "checkpoint_status", checkpoint_status)

        states = tuple(self.step_states)
        if not states:
            raise ExecutionCheckpointError(
                "checkpoint must contain at least one step state"
            )
        if not all(isinstance(item, StepCheckpointState) for item in states):
            raise ExecutionCheckpointError(
                "step_states must contain StepCheckpointState values"
            )

        sorted_states = tuple(sorted(states, key=lambda item: item.step_id))
        step_ids = tuple(item.step_id for item in sorted_states)
        if len(step_ids) != len(set(step_ids)):
            raise ExecutionCheckpointError(
                "checkpoint step identifiers must be unique"
            )
        object.__setattr__(self, "step_states", sorted_states)

        object.__setattr__(
            self,
            "plan_definition_hash",
            _hash(self.plan_definition_hash, "plan_definition_hash"),
        )
        object.__setattr__(
            self,
            "plan_state_hash",
            _hash(self.plan_state_hash, "plan_state_hash"),
        )

        if (
            isinstance(self.journal_event_count, bool)
            or not isinstance(self.journal_event_count, int)
            or self.journal_event_count < 0
        ):
            raise ExecutionCheckpointError(
                "journal_event_count must be non-negative"
            )
        object.__setattr__(
            self,
            "journal_head_hash",
            _hash(self.journal_head_hash, "journal_head_hash"),
        )
        object.__setattr__(
            self,
            "journal_hash",
            _hash(self.journal_hash, "journal_hash"),
        )

        if self.version != CHECKPOINT_FORMAT_VERSION:
            raise ExecutionCheckpointError(
                "unsupported checkpoint format version"
            )

        expected_status = (
            CheckpointStatus.TERMINAL
            if plan_status in {PlanStatus.FAILED, PlanStatus.COMPLETED}
            else CheckpointStatus.BLOCKED
            if plan_status is PlanStatus.BLOCKED
            else CheckpointStatus.READY
        )
        if checkpoint_status is not expected_status:
            raise ExecutionCheckpointError(
                "checkpoint_status is inconsistent with plan_status"
            )

        computed = self.compute_hash()
        if self.checkpoint_hash is None:
            object.__setattr__(self, "checkpoint_hash", computed)
        else:
            supplied = _hash(self.checkpoint_hash, "checkpoint_hash")
            if supplied != computed:
                raise CheckpointIntegrityError(
                    "checkpoint hash does not match checkpoint content"
                )
            object.__setattr__(self, "checkpoint_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "execution_checkpoint",
            "version": self.version,
            "checkpoint_id": self.checkpoint_id,
            "created_at": self.created_at,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "plan_status": self.plan_status.value,
            "checkpoint_status": self.checkpoint_status.value,
            "step_states": [
                state.to_dict()
                for state in self.step_states
            ],
            "plan_definition_hash": self.plan_definition_hash,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256(self.hash_material())

    def verify_hash(self) -> None:
        if self.checkpoint_hash != self.compute_hash():
            raise CheckpointIntegrityError(
                "checkpoint hash does not match checkpoint content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["checkpoint_hash"] = self.checkpoint_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        *,
        checkpoint_id: str,
        created_at: str | datetime,
    ) -> "ExecutionCheckpoint":
        _validate_plan_journal_compatibility(plan, journal)
        seal = journal.seal()

        return cls(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            plan_status=plan.status,
            checkpoint_status=_checkpoint_status(plan),
            step_states=tuple(
                StepCheckpointState(
                    step_id=step.step_id,
                    status=step.status,
                    assigned_agent_id=step.assigned_agent_id,
                    approval_reference=step.approval_reference,
                )
                for step in plan.steps
            ),
            plan_definition_hash=_plan_definition_hash(plan),
            plan_state_hash=_plan_state_hash(plan),
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ExecutionCheckpoint":
        if data.get("record_type") != "execution_checkpoint":
            raise ExecutionCheckpointError(
                "record_type must be execution_checkpoint"
            )
        if "checkpoint_hash" not in data:
            raise CheckpointIntegrityError(
                "serialized checkpoint is missing checkpoint_hash"
            )
        raw_states = data.get("step_states")
        if not isinstance(raw_states, list):
            raise ExecutionCheckpointError(
                "step_states must be a list"
            )

        return cls(
            checkpoint_id=data["checkpoint_id"],
            created_at=data["created_at"],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            plan_status=PlanStatus(data["plan_status"]),
            checkpoint_status=CheckpointStatus(data["checkpoint_status"]),
            step_states=tuple(
                StepCheckpointState.from_dict(item)
                for item in raw_states
            ),
            plan_definition_hash=data["plan_definition_hash"],
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            checkpoint_hash=data["checkpoint_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionCheckpoint":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionCheckpointError(
                "checkpoint JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionCheckpointError(
                "checkpoint JSON must be an object"
            )
        return cls.from_dict(data)

    def assess_resume(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> ResumeAssessment:
        self.verify_hash()

        if not isinstance(plan, ExecutionPlan):
            raise ExecutionCheckpointError("plan must be an ExecutionPlan")
        if not isinstance(journal, ExecutionJournal):
            raise ExecutionCheckpointError(
                "journal must be an ExecutionJournal"
            )

        reasons: list[str] = []

        if plan.plan_id != self.plan_id or journal.plan_id != self.plan_id:
            reasons.append("plan or journal identifier differs from checkpoint")
            return self._assessment(
                ResumeAssessmentStatus.INCOMPATIBLE,
                False,
                reasons,
                plan,
                journal,
            )

        if plan.project_id != self.project_id:
            reasons.append("project identifier differs from checkpoint")
            return self._assessment(
                ResumeAssessmentStatus.INCOMPATIBLE,
                False,
                reasons,
                plan,
                journal,
            )

        try:
            _validate_plan_journal_compatibility(plan, journal)
        except (
            PlanJournalCompatibilityError,
            JournalIntegrityError,
        ) as exc:
            reasons.append(str(exc))
            return self._assessment(
                ResumeAssessmentStatus.INCOMPATIBLE,
                False,
                reasons,
                plan,
                journal,
            )

        if _plan_definition_hash(plan) != self.plan_definition_hash:
            reasons.append("plan definition differs from checkpoint")
            return self._assessment(
                ResumeAssessmentStatus.INCOMPATIBLE,
                False,
                reasons,
                plan,
                journal,
            )

        current_seal = journal.seal()

        if current_seal.event_count < self.journal_event_count:
            reasons.append("journal contains fewer events than checkpoint")
            return self._assessment(
                ResumeAssessmentStatus.INCOMPATIBLE,
                False,
                reasons,
                plan,
                journal,
            )

        if current_seal.event_count == self.journal_event_count:
            if (
                current_seal.head_hash != self.journal_head_hash
                or current_seal.journal_hash != self.journal_hash
            ):
                reasons.append("journal seal diverges from checkpoint")
                return self._assessment(
                    ResumeAssessmentStatus.INCOMPATIBLE,
                    False,
                    reasons,
                    plan,
                    journal,
                )
            if _plan_state_hash(plan) != self.plan_state_hash:
                reasons.append(
                    "plan state changed without journal advancement"
                )
                return self._assessment(
                    ResumeAssessmentStatus.INCOMPATIBLE,
                    False,
                    reasons,
                    plan,
                    journal,
                )
        else:
            prefix = ExecutionJournal.from_events(
                journal.plan_id,
                journal.events[: self.journal_event_count],
            )
            prefix_seal = prefix.seal()
            if (
                prefix_seal.head_hash != self.journal_head_hash
                or prefix_seal.journal_hash != self.journal_hash
            ):
                reasons.append(
                    "journal history diverges before checkpoint boundary"
                )
                return self._assessment(
                    ResumeAssessmentStatus.INCOMPATIBLE,
                    False,
                    reasons,
                    plan,
                    journal,
                )
            reasons.append("checkpoint is older than the current journal")
            return self._assessment(
                ResumeAssessmentStatus.STALE,
                False,
                reasons,
                plan,
                journal,
            )

        if self.checkpoint_status is CheckpointStatus.TERMINAL:
            reasons.append("checkpoint represents a terminal plan")
            return self._assessment(
                ResumeAssessmentStatus.TERMINAL,
                False,
                reasons,
                plan,
                journal,
            )

        if self.checkpoint_status is CheckpointStatus.BLOCKED:
            reasons.append("checkpoint represents a blocked plan")
            return self._assessment(
                ResumeAssessmentStatus.BLOCKED,
                False,
                reasons,
                plan,
                journal,
            )

        reasons.append("checkpoint, plan, and journal are compatible")
        return self._assessment(
            ResumeAssessmentStatus.READY,
            True,
            reasons,
            plan,
            journal,
        )

    def _assessment(
        self,
        status: ResumeAssessmentStatus,
        can_resume: bool,
        reasons: list[str],
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> ResumeAssessment:
        ready = tuple(
            step.step_id
            for step in plan.ready_steps()
        )
        running = tuple(
            step.step_id
            for step in plan.steps
            if step.status is StepStatus.RUNNING
        )
        return ResumeAssessment(
            checkpoint_id=self.checkpoint_id,
            status=status,
            can_resume=can_resume,
            reasons=tuple(reasons),
            ready_step_ids=ready,
            running_step_ids=running,
            current_event_count=journal.event_count,
            current_head_hash=journal.head_hash,
        )
