"""Deterministic in-memory application of resume commands for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .agent_contracts import canonical_json
from .execution_checkpoint import (
    ExecutionCheckpoint,
    ResumeAssessmentStatus,
)
from .execution_journal import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionJournal,
)
from .execution_plan import (
    ExecutionPlan,
    PlanStatus,
    StepStatus,
)
from .execution_resume import ResumeCommand


RESUME_APPLICATION_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")

_PAYLOAD_APPLICATION_ID = "resume_application_id"
_PAYLOAD_COMMAND_ID = "resume_command_id"
_PAYLOAD_COMMAND_HASH = "resume_command_hash"
_PAYLOAD_CHECKPOINT_ID = "resume_checkpoint_id"
_PAYLOAD_CHECKPOINT_HASH = "resume_checkpoint_hash"
_PAYLOAD_APPROVAL_REFERENCE = "resume_approval_reference"
_PAYLOAD_SELECTED_STEP_IDS = "resume_selected_step_ids"


class ResumeApplicationError(ValueError):
    """A resume command cannot be applied safely to the supplied state."""


class ResumeApplicationIntegrityError(ResumeApplicationError):
    """An application receipt, command marker, or state hash is invalid."""


class ResumeApplicationConflictError(ResumeApplicationError):
    """A command identifier or application marker conflicts with history."""


class ResumeApplicationStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already-applied"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResumeApplicationError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(value: object, name: str) -> str:
    result = _text(value, name)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ResumeApplicationError(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ResumeApplicationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _sequence_tuple(values: object) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise ResumeApplicationError(
            "appended_event_sequences must be an iterable"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ResumeApplicationError(
            "appended_event_sequences must be an iterable"
        ) from exc

    normalized: list[int] = []
    for value in items:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ResumeApplicationError(
                "appended event sequences must be positive integers"
            )
        normalized.append(value)

    if tuple(sorted(set(normalized))) != tuple(normalized):
        raise ResumeApplicationError(
            "appended event sequences must be unique and increasing"
        )
    return tuple(normalized)


def _step_ids(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ResumeApplicationError(
            "selected_step_ids must be an iterable"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ResumeApplicationError(
            "selected_step_ids must be an iterable"
        ) from exc

    normalized_items: list[str] = []
    for item in items:
        value = _text(item, "step_id")
        if _STEP_ID.fullmatch(value) is None:
            raise ResumeApplicationError(
                "step_id has an invalid format"
            )
        normalized_items.append(value)

    normalized = tuple(sorted(set(normalized_items)))
    if not normalized:
        raise ResumeApplicationError(
            "selected_step_ids must contain at least one step"
        )
    return normalized


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _plan_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


def _application_id(command_hash: str) -> str:
    return f"application:{command_hash}"


def _marker_payload(
    command: ResumeCommand,
    application_id: str,
    *,
    selected_step_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    command_hash = command.command_hash
    assert command_hash is not None

    payload: dict[str, Any] = {
        _PAYLOAD_APPLICATION_ID: application_id,
        _PAYLOAD_COMMAND_ID: command.command_id,
        _PAYLOAD_COMMAND_HASH: command_hash,
        _PAYLOAD_CHECKPOINT_ID: command.checkpoint_id,
        _PAYLOAD_CHECKPOINT_HASH: command.checkpoint_hash,
        _PAYLOAD_APPROVAL_REFERENCE: command.approval_reference,
    }
    if selected_step_ids is not None:
        payload[_PAYLOAD_SELECTED_STEP_IDS] = list(selected_step_ids)
    return payload


def _event_command_hash(event: ExecutionEvent) -> str | None:
    value = event.payload.get(_PAYLOAD_COMMAND_HASH)
    return value if isinstance(value, str) else None


def _event_command_id(event: ExecutionEvent) -> str | None:
    value = event.payload.get(_PAYLOAD_COMMAND_ID)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class ResumeApplicationResult:
    application_id: str
    status: ResumeApplicationStatus
    command_id: str
    command_hash: str
    checkpoint_id: str
    checkpoint_hash: str
    plan_id: str
    selected_step_ids: tuple[str, ...]
    appended_event_sequences: tuple[int, ...]
    plan_before_hash: str
    plan_after_hash: str
    journal_before_event_count: int
    journal_after_event_count: int
    journal_before_head_hash: str
    journal_after_head_hash: str
    journal_before_hash: str
    journal_after_hash: str
    applied_at: str
    updated_plan: ExecutionPlan
    updated_events: tuple[ExecutionEvent, ...] = field(repr=False)
    result_hash: str | None = None
    version: int = RESUME_APPLICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "application_id",
            "command_id",
            "checkpoint_id",
            "plan_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )

        try:
            status = ResumeApplicationStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ResumeApplicationError(
                "resume application status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        for field_name in (
            "command_hash",
            "checkpoint_hash",
            "plan_before_hash",
            "plan_after_hash",
            "journal_before_head_hash",
            "journal_after_head_hash",
            "journal_before_hash",
            "journal_after_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )

        object.__setattr__(
            self,
            "selected_step_ids",
            _step_ids(self.selected_step_ids),
        )
        if self.application_id != _application_id(self.command_hash):
            raise ResumeApplicationError(
                "application_id does not match command_hash"
            )
        object.__setattr__(
            self,
            "appended_event_sequences",
            _sequence_tuple(self.appended_event_sequences),
        )
        object.__setattr__(
            self,
            "applied_at",
            _text(self.applied_at, "applied_at"),
        )

        for field_name in (
            "journal_before_event_count",
            "journal_after_event_count",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ResumeApplicationError(
                    f"{field_name} must be a non-negative integer"
                )

        if not isinstance(self.updated_plan, ExecutionPlan):
            raise ResumeApplicationError(
                "updated_plan must be an ExecutionPlan"
            )
        if self.updated_plan.plan_id != self.plan_id:
            raise ResumeApplicationError(
                "updated plan identifier does not match result plan_id"
            )

        events = tuple(self.updated_events)
        if not all(isinstance(event, ExecutionEvent) for event in events):
            raise ResumeApplicationError(
                "updated_events must contain ExecutionEvent values"
            )
        object.__setattr__(self, "updated_events", events)

        reconstructed = ExecutionJournal.from_events(
            self.plan_id,
            events,
        )
        seal = reconstructed.seal()

        if _plan_hash(self.updated_plan) != self.plan_after_hash:
            raise ResumeApplicationIntegrityError(
                "updated plan does not match plan_after_hash"
            )
        if reconstructed.event_count != self.journal_after_event_count:
            raise ResumeApplicationIntegrityError(
                "updated journal count does not match result"
            )
        if reconstructed.head_hash != self.journal_after_head_hash:
            raise ResumeApplicationIntegrityError(
                "updated journal head does not match result"
            )
        if seal.journal_hash != self.journal_after_hash:
            raise ResumeApplicationIntegrityError(
                "updated journal hash does not match result"
            )

        if status is ResumeApplicationStatus.APPLIED:
            if not self.appended_event_sequences:
                raise ResumeApplicationError(
                    "applied result must list appended event sequences"
                )
            expected = tuple(
                range(
                    self.journal_before_event_count + 1,
                    self.journal_after_event_count + 1,
                )
            )
            if self.appended_event_sequences != expected:
                raise ResumeApplicationError(
                    "appended event sequences are not contiguous"
                )
            if (
                self.journal_after_event_count
                <= self.journal_before_event_count
            ):
                raise ResumeApplicationError(
                    "applied result must advance the journal"
                )
        else:
            if self.appended_event_sequences:
                raise ResumeApplicationError(
                    "already-applied result cannot append events"
                )
            if (
                self.journal_after_event_count
                != self.journal_before_event_count
            ):
                raise ResumeApplicationError(
                    "already-applied result cannot change event count"
                )
            if self.plan_after_hash != self.plan_before_hash:
                raise ResumeApplicationError(
                    "already-applied result cannot change the plan"
                )
            if (
                self.journal_after_head_hash
                != self.journal_before_head_hash
                or self.journal_after_hash
                != self.journal_before_hash
            ):
                raise ResumeApplicationError(
                    "already-applied result cannot change the journal"
                )

        if self.version != RESUME_APPLICATION_FORMAT_VERSION:
            raise ResumeApplicationError(
                "unsupported resume application format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ResumeApplicationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "resume_application_result",
            "version": self.version,
            "application_id": self.application_id,
            "status": self.status.value,
            "command_id": self.command_id,
            "command_hash": self.command_hash,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_hash": self.checkpoint_hash,
            "plan_id": self.plan_id,
            "selected_step_ids": list(self.selected_step_ids),
            "appended_event_sequences": list(
                self.appended_event_sequences
            ),
            "plan_before_hash": self.plan_before_hash,
            "plan_after_hash": self.plan_after_hash,
            "journal_before_event_count": (
                self.journal_before_event_count
            ),
            "journal_after_event_count": (
                self.journal_after_event_count
            ),
            "journal_before_head_hash": (
                self.journal_before_head_hash
            ),
            "journal_after_head_hash": (
                self.journal_after_head_hash
            ),
            "journal_before_hash": self.journal_before_hash,
            "journal_after_hash": self.journal_after_hash,
            "applied_at": self.applied_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ResumeApplicationIntegrityError(
                "result hash does not match result content"
            )

    def to_journal(self) -> ExecutionJournal:
        return ExecutionJournal.from_events(
            self.plan_id,
            self.updated_events,
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["result_hash"] = self.result_hash
        data["updated_plan"] = self.updated_plan.to_dict()
        data["updated_events"] = [
            event.to_dict()
            for event in self.updated_events
        ]
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ResumeApplicationResult":
        if data.get("record_type") != "resume_application_result":
            raise ResumeApplicationError(
                "record_type must be resume_application_result"
            )
        if "result_hash" not in data:
            raise ResumeApplicationIntegrityError(
                "serialized result is missing result_hash"
            )
        raw_plan = data.get("updated_plan")
        raw_events = data.get("updated_events")
        if not isinstance(raw_plan, Mapping):
            raise ResumeApplicationError(
                "updated_plan must be an object"
            )
        if not isinstance(raw_events, list):
            raise ResumeApplicationError(
                "updated_events must be a list"
            )

        return cls(
            application_id=data["application_id"],
            status=ResumeApplicationStatus(data["status"]),
            command_id=data["command_id"],
            command_hash=data["command_hash"],
            checkpoint_id=data["checkpoint_id"],
            checkpoint_hash=data["checkpoint_hash"],
            plan_id=data["plan_id"],
            selected_step_ids=tuple(data["selected_step_ids"]),
            appended_event_sequences=tuple(
                data["appended_event_sequences"]
            ),
            plan_before_hash=data["plan_before_hash"],
            plan_after_hash=data["plan_after_hash"],
            journal_before_event_count=(
                data["journal_before_event_count"]
            ),
            journal_after_event_count=(
                data["journal_after_event_count"]
            ),
            journal_before_head_hash=(
                data["journal_before_head_hash"]
            ),
            journal_after_head_hash=(
                data["journal_after_head_hash"]
            ),
            journal_before_hash=data["journal_before_hash"],
            journal_after_hash=data["journal_after_hash"],
            applied_at=data["applied_at"],
            updated_plan=ExecutionPlan.from_dict(raw_plan),
            updated_events=tuple(
                ExecutionEvent.from_dict(event)
                for event in raw_events
            ),
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ResumeApplicationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ResumeApplicationError(
                "resume application result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ResumeApplicationError(
                "resume application result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ResumeApplication:
    command: ResumeCommand
    checkpoint: ExecutionCheckpoint

    def __post_init__(self) -> None:
        if not isinstance(self.command, ResumeCommand):
            raise ResumeApplicationError(
                "command must be a ResumeCommand"
            )
        if not isinstance(self.checkpoint, ExecutionCheckpoint):
            raise ResumeApplicationError(
                "checkpoint must be an ExecutionCheckpoint"
            )

        self.command.verify_hash()
        self.checkpoint.verify_hash()

        checkpoint_hash = self.checkpoint.checkpoint_hash
        assert checkpoint_hash is not None

        if self.command.checkpoint_id != self.checkpoint.checkpoint_id:
            raise ResumeApplicationError(
                "command checkpoint identifier does not match checkpoint"
            )
        if self.command.checkpoint_hash != checkpoint_hash:
            raise ResumeApplicationError(
                "command checkpoint hash does not match checkpoint"
            )
        if self.command.plan_id != self.checkpoint.plan_id:
            raise ResumeApplicationError(
                "command plan identifier does not match checkpoint"
            )

    @property
    def application_id(self) -> str:
        command_hash = self.command.command_hash
        assert command_hash is not None
        return _application_id(command_hash)

    def apply(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> ResumeApplicationResult:
        if not isinstance(plan, ExecutionPlan):
            raise ResumeApplicationError(
                "plan must be an ExecutionPlan"
            )
        if not isinstance(journal, ExecutionJournal):
            raise ResumeApplicationError(
                "journal must be an ExecutionJournal"
            )
        if plan.plan_id != self.command.plan_id:
            raise ResumeApplicationError(
                "plan identifier does not match command"
            )
        if journal.plan_id != self.command.plan_id:
            raise ResumeApplicationError(
                "journal identifier does not match command"
            )

        self.command.verify_hash()
        self.checkpoint.verify_hash()
        journal.validate()

        existing = self._existing_application_events(journal)
        if existing:
            return self._already_applied_result(
                plan,
                journal,
                existing,
            )

        assessment = self.checkpoint.assess_resume(plan, journal)
        if (
            assessment.status is not ResumeAssessmentStatus.READY
            or not assessment.can_resume
        ):
            raise ResumeApplicationError(
                "checkpoint is not current and resumable"
            )

        if plan.status not in {
            PlanStatus.PENDING,
            PlanStatus.APPROVED,
            PlanStatus.RUNNING,
        }:
            raise ResumeApplicationError(
                "plan status cannot accept a resume command"
            )

        ready_ids = {
            step.step_id
            for step in plan.ready_steps()
        }
        requested_ids = set(self.command.selected_step_ids)
        unavailable = tuple(sorted(requested_ids - ready_ids))
        if unavailable:
            raise ResumeApplicationError(
                "command selects non-ready steps: "
                + ", ".join(unavailable)
            )

        before_plan_hash = _plan_hash(plan)
        before_seal = journal.seal()
        updated_plan = self._updated_plan(plan)
        updated_journal = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events,
        )

        appended: list[int] = []
        common_payload = _marker_payload(
            self.command,
            self.application_id,
        )

        if plan.status is PlanStatus.PENDING:
            event = updated_journal.append(
                ExecutionEventType.PLAN_APPROVED,
                self.command.issued_at,
                payload=_marker_payload(
                    self.command,
                    self.application_id,
                    selected_step_ids=self.command.selected_step_ids,
                ),
            )
            appended.append(event.sequence)

        for step_id in self.command.selected_step_ids:
            event = updated_journal.append(
                ExecutionEventType.STEP_APPROVED,
                self.command.issued_at,
                step_id=step_id,
                payload=common_payload,
            )
            appended.append(event.sequence)

        ExecutionCheckpoint.capture(
            updated_plan,
            updated_journal,
            checkpoint_id=f"validation:{self.command.command_hash}",
            created_at=self.command.issued_at,
        )

        after_seal = updated_journal.seal()
        return ResumeApplicationResult(
            application_id=self.application_id,
            status=ResumeApplicationStatus.APPLIED,
            command_id=self.command.command_id,
            command_hash=self.command.command_hash or "",
            checkpoint_id=self.checkpoint.checkpoint_id,
            checkpoint_hash=self.command.checkpoint_hash,
            plan_id=self.command.plan_id,
            selected_step_ids=self.command.selected_step_ids,
            appended_event_sequences=tuple(appended),
            plan_before_hash=before_plan_hash,
            plan_after_hash=_plan_hash(updated_plan),
            journal_before_event_count=before_seal.event_count,
            journal_after_event_count=after_seal.event_count,
            journal_before_head_hash=before_seal.head_hash,
            journal_after_head_hash=after_seal.head_hash,
            journal_before_hash=before_seal.journal_hash,
            journal_after_hash=after_seal.journal_hash,
            applied_at=self.command.issued_at,
            updated_plan=updated_plan,
            updated_events=updated_journal.events,
        )

    def _updated_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        selected = set(self.command.selected_step_ids)
        updated_steps = []

        for step in plan.steps:
            if step.step_id not in selected:
                updated_steps.append(step)
                continue

            if step.status not in {
                StepStatus.PENDING,
                StepStatus.APPROVED,
            }:
                raise ResumeApplicationError(
                    f"{step.step_id} cannot be prepared for resume"
                )
            if (
                step.status is StepStatus.APPROVED
                and step.approval_reference is not None
                and step.approval_reference
                != self.command.approval_reference
            ):
                raise ResumeApplicationConflictError(
                    f"{step.step_id} has a conflicting approval reference"
                )

            updated_steps.append(
                step.with_status(
                    StepStatus.APPROVED,
                    approval_reference=self.command.approval_reference,
                )
            )

        effective_status = (
            PlanStatus.APPROVED
            if plan.status is PlanStatus.PENDING
            else plan.status
        )
        effective_plan_approval = (
            self.command.approval_reference
            if plan.status is PlanStatus.PENDING
            else plan.approval_reference
        )

        return replace(
            plan,
            steps=tuple(updated_steps),
            status=effective_status,
            approval_reference=effective_plan_approval,
        )

    def _existing_application_events(
        self,
        journal: ExecutionJournal,
    ) -> tuple[ExecutionEvent, ...]:
        command_hash = self.command.command_hash
        assert command_hash is not None

        for event in journal.events:
            event_command_id = _event_command_id(event)
            event_command_hash = _event_command_hash(event)
            if event_command_id == self.command.command_id:
                if event_command_hash != command_hash:
                    raise ResumeApplicationConflictError(
                        "command identifier already exists with another hash"
                    )

        matches = tuple(
            event
            for event in journal.events
            if _event_command_hash(event) == command_hash
        )
        if not matches:
            return ()

        expected_count = len(self.command.selected_step_ids)
        if self.checkpoint.plan_status is PlanStatus.PENDING:
            expected_count += 1

        if len(matches) != expected_count:
            raise ResumeApplicationIntegrityError(
                "resume application marker count is incomplete or duplicated"
            )

        expected_sequences = tuple(
            range(
                self.checkpoint.journal_event_count + 1,
                self.checkpoint.journal_event_count
                + expected_count
                + 1,
            )
        )
        actual_sequences = tuple(event.sequence for event in matches)
        if actual_sequences != expected_sequences:
            raise ResumeApplicationIntegrityError(
                "resume application markers are not contiguous at checkpoint boundary"
            )

        step_ids: list[str] = []
        plan_markers = 0
        for event in matches:
            payload = event.payload
            expected_values = {
                _PAYLOAD_APPLICATION_ID: self.application_id,
                _PAYLOAD_COMMAND_ID: self.command.command_id,
                _PAYLOAD_COMMAND_HASH: command_hash,
                _PAYLOAD_CHECKPOINT_ID: self.command.checkpoint_id,
                _PAYLOAD_CHECKPOINT_HASH: self.command.checkpoint_hash,
                _PAYLOAD_APPROVAL_REFERENCE: (
                    self.command.approval_reference
                ),
            }
            for key, expected in expected_values.items():
                if payload.get(key) != expected:
                    raise ResumeApplicationIntegrityError(
                        f"resume marker field {key} does not match command"
                    )

            if event.event_type is ExecutionEventType.PLAN_APPROVED:
                plan_markers += 1
                selected = payload.get(_PAYLOAD_SELECTED_STEP_IDS)
                if not isinstance(selected, tuple) or selected != self.command.selected_step_ids:
                    raise ResumeApplicationIntegrityError(
                        "plan resume marker selection does not match command"
                    )
                continue

            if event.event_type is not ExecutionEventType.STEP_APPROVED:
                raise ResumeApplicationIntegrityError(
                    "resume marker uses an unexpected event type"
                )
            if event.step_id is None:
                raise ResumeApplicationIntegrityError(
                    "resume step marker is missing step_id"
                )
            step_ids.append(event.step_id)

        expected_plan_markers = (
            1
            if self.checkpoint.plan_status is PlanStatus.PENDING
            else 0
        )
        if plan_markers != expected_plan_markers:
            raise ResumeApplicationIntegrityError(
                "plan approval marker count does not match checkpoint state"
            )
        if tuple(step_ids) != self.command.selected_step_ids:
            raise ResumeApplicationIntegrityError(
                "resume step markers do not match command selection"
            )

        return matches

    def _already_applied_result(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        existing: tuple[ExecutionEvent, ...],
    ) -> ResumeApplicationResult:
        assessment = self.checkpoint.assess_resume(plan, journal)
        if assessment.status is not ResumeAssessmentStatus.STALE:
            raise ResumeApplicationIntegrityError(
                "existing command markers are incompatible with current state"
            )

        current_plan_hash = _plan_hash(plan)
        current_seal = journal.seal()
        return ResumeApplicationResult(
            application_id=self.application_id,
            status=ResumeApplicationStatus.ALREADY_APPLIED,
            command_id=self.command.command_id,
            command_hash=self.command.command_hash or "",
            checkpoint_id=self.checkpoint.checkpoint_id,
            checkpoint_hash=self.command.checkpoint_hash,
            plan_id=self.command.plan_id,
            selected_step_ids=self.command.selected_step_ids,
            appended_event_sequences=(),
            plan_before_hash=current_plan_hash,
            plan_after_hash=current_plan_hash,
            journal_before_event_count=current_seal.event_count,
            journal_after_event_count=current_seal.event_count,
            journal_before_head_hash=current_seal.head_hash,
            journal_after_head_hash=current_seal.head_hash,
            journal_before_hash=current_seal.journal_hash,
            journal_after_hash=current_seal.journal_hash,
            applied_at=existing[0].timestamp,
            updated_plan=plan,
            updated_events=journal.events,
        )
