"""Controlled, declarative resume authorization for ELMAN-OS v0.7."""

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
from .execution_checkpoint import (
    CheckpointStatus,
    ExecutionCheckpoint,
    ResumeAssessment,
    ResumeAssessmentStatus,
)
from .execution_plan import StepStatus


RESUME_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionResumeError(ValueError):
    """A resume request, policy, decision, or command is malformed."""


class ResumeIntegrityError(ExecutionResumeError):
    """A serialized resume artifact fails its integrity check."""


class ResumeDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ResumeSelectionStrategy(StrEnum):
    READY_ONLY = "ready-only"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionResumeError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ExecutionResumeError(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ExecutionResumeError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _step_ids(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ExecutionResumeError(f"{name} must be an iterable")
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExecutionResumeError(
            f"{name} must be an iterable"
        ) from exc
    return tuple(
        sorted(
            {
                _identifier(item, name, _STEP_ID)
                for item in items
            }
        )
    )


def _reasons(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ExecutionResumeError("reasons must be an iterable")
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExecutionResumeError(
            "reasons must be an iterable"
        ) from exc
    normalized = tuple(_text(item, "reason") for item in items)
    if not normalized:
        raise ExecutionResumeError(
            "decision must contain at least one reason"
        )
    return normalized


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ExecutionResumeError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ExecutionResumeError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ExecutionResumeError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ExecutionResumeError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ExecutionResumeError(f"{name} must be UTC")
    else:
        raise ExecutionResumeError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResumePolicy:
    policy_id: str
    strategy: ResumeSelectionStrategy = ResumeSelectionStrategy.READY_ONLY
    require_human_approval: bool = True
    allowed_step_ids: tuple[str, ...] = ()
    max_steps: int | None = None
    version: int = RESUME_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        try:
            strategy = ResumeSelectionStrategy(self.strategy)
        except (TypeError, ValueError) as exc:
            raise ExecutionResumeError(
                "resume strategy is invalid"
            ) from exc
        object.__setattr__(self, "strategy", strategy)

        if self.require_human_approval is not True:
            raise ExecutionResumeError(
                "resume policy must require human approval"
            )

        object.__setattr__(
            self,
            "allowed_step_ids",
            _step_ids(self.allowed_step_ids, "allowed_step_ids"),
        )

        if self.max_steps is not None:
            if (
                isinstance(self.max_steps, bool)
                or not isinstance(self.max_steps, int)
                or self.max_steps < 1
            ):
                raise ExecutionResumeError(
                    "max_steps must be a positive integer or null"
                )

        if self.version != RESUME_FORMAT_VERSION:
            raise ExecutionResumeError(
                "unsupported resume format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "resume_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "strategy": self.strategy.value,
            "require_human_approval": self.require_human_approval,
            "allowed_step_ids": list(self.allowed_step_ids),
            "max_steps": self.max_steps,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumePolicy":
        if data.get("record_type") != "resume_policy":
            raise ExecutionResumeError(
                "record_type must be resume_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            strategy=ResumeSelectionStrategy(data["strategy"]),
            require_human_approval=data["require_human_approval"],
            allowed_step_ids=tuple(data.get("allowed_step_ids", ())),
            max_steps=data.get("max_steps"),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ResumePolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionResumeError(
                "resume policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionResumeError(
                "resume policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    request_id: str
    checkpoint_id: str
    checkpoint_hash: str
    plan_id: str
    requested_by: str
    approval_reference: str
    created_at: str
    rationale: str
    requested_step_ids: tuple[str, ...] = ()
    version: int = RESUME_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "checkpoint_id",
            _identifier(self.checkpoint_id, "checkpoint_id"),
        )
        object.__setattr__(
            self,
            "checkpoint_hash",
            _hash(self.checkpoint_hash, "checkpoint_hash"),
        )
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id"),
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "approval_reference",
            _identifier(
                self.approval_reference,
                "approval_reference",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _utc_timestamp(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "rationale",
            _text(self.rationale, "rationale"),
        )
        object.__setattr__(
            self,
            "requested_step_ids",
            _step_ids(
                self.requested_step_ids,
                "requested_step_ids",
            ),
        )

        if self.version != RESUME_FORMAT_VERSION:
            raise ExecutionResumeError(
                "unsupported resume format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "resume_request",
            "version": self.version,
            "request_id": self.request_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_hash": self.checkpoint_hash,
            "plan_id": self.plan_id,
            "requested_by": self.requested_by,
            "approval_reference": self.approval_reference,
            "created_at": self.created_at,
            "rationale": self.rationale,
            "requested_step_ids": list(self.requested_step_ids),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def request_hash(self) -> str:
        return _sha256(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeRequest":
        if data.get("record_type") != "resume_request":
            raise ExecutionResumeError(
                "record_type must be resume_request"
            )
        return cls(
            request_id=data["request_id"],
            checkpoint_id=data["checkpoint_id"],
            checkpoint_hash=data["checkpoint_hash"],
            plan_id=data["plan_id"],
            requested_by=data["requested_by"],
            approval_reference=data["approval_reference"],
            created_at=data["created_at"],
            rationale=data["rationale"],
            requested_step_ids=tuple(data.get("requested_step_ids", ())),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ResumeRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionResumeError(
                "resume request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionResumeError(
                "resume request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    command_id: str
    request_id: str
    request_hash: str
    policy_id: str
    policy_hash: str
    checkpoint_id: str
    checkpoint_hash: str
    plan_id: str
    approval_reference: str
    selected_step_ids: tuple[str, ...]
    issued_at: str
    command_hash: str | None = None
    version: int = RESUME_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "command_id",
            "request_id",
            "policy_id",
            "checkpoint_id",
            "plan_id",
            "approval_reference",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )

        object.__setattr__(
            self,
            "request_hash",
            _hash(self.request_hash, "request_hash"),
        )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        object.__setattr__(
            self,
            "checkpoint_hash",
            _hash(self.checkpoint_hash, "checkpoint_hash"),
        )
        selected = _step_ids(
            self.selected_step_ids,
            "selected_step_ids",
        )
        if not selected:
            raise ExecutionResumeError(
                "resume command must select at least one step"
            )
        object.__setattr__(self, "selected_step_ids", selected)
        object.__setattr__(
            self,
            "issued_at",
            _utc_timestamp(self.issued_at, "issued_at"),
        )

        if self.version != RESUME_FORMAT_VERSION:
            raise ExecutionResumeError(
                "unsupported resume format version"
            )

        computed = self.compute_hash()
        if self.command_hash is None:
            object.__setattr__(self, "command_hash", computed)
        else:
            supplied = _hash(self.command_hash, "command_hash")
            if supplied != computed:
                raise ResumeIntegrityError(
                    "command hash does not match command content"
                )
            object.__setattr__(self, "command_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "resume_command",
            "version": self.version,
            "command_id": self.command_id,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_hash": self.checkpoint_hash,
            "plan_id": self.plan_id,
            "approval_reference": self.approval_reference,
            "selected_step_ids": list(self.selected_step_ids),
            "issued_at": self.issued_at,
        }

    def compute_hash(self) -> str:
        return _sha256(self.hash_material())

    def verify_hash(self) -> None:
        if self.command_hash != self.compute_hash():
            raise ResumeIntegrityError(
                "command hash does not match command content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["command_hash"] = self.command_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeCommand":
        if data.get("record_type") != "resume_command":
            raise ExecutionResumeError(
                "record_type must be resume_command"
            )
        if "command_hash" not in data:
            raise ResumeIntegrityError(
                "serialized command is missing command_hash"
            )
        return cls(
            command_id=data["command_id"],
            request_id=data["request_id"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            checkpoint_id=data["checkpoint_id"],
            checkpoint_hash=data["checkpoint_hash"],
            plan_id=data["plan_id"],
            approval_reference=data["approval_reference"],
            selected_step_ids=tuple(data["selected_step_ids"]),
            issued_at=data["issued_at"],
            command_hash=data["command_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ResumeCommand":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionResumeError(
                "resume command JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionResumeError(
                "resume command JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    decision_id: str
    request_id: str
    request_hash: str
    policy_id: str
    policy_hash: str
    checkpoint_id: str
    checkpoint_hash: str
    status: ResumeDecisionStatus
    reasons: tuple[str, ...]
    selected_step_ids: tuple[str, ...]
    issued_at: str
    command: ResumeCommand | None
    decision_hash: str | None = None
    version: int = RESUME_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id",
            "request_id",
            "policy_id",
            "checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )

        object.__setattr__(
            self,
            "request_hash",
            _hash(self.request_hash, "request_hash"),
        )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        object.__setattr__(
            self,
            "checkpoint_hash",
            _hash(self.checkpoint_hash, "checkpoint_hash"),
        )

        try:
            status = ResumeDecisionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ExecutionResumeError(
                "resume decision status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        object.__setattr__(self, "reasons", _reasons(self.reasons))
        selected = _step_ids(
            self.selected_step_ids,
            "selected_step_ids",
        )
        object.__setattr__(self, "selected_step_ids", selected)
        object.__setattr__(
            self,
            "issued_at",
            _utc_timestamp(self.issued_at, "issued_at"),
        )

        if self.version != RESUME_FORMAT_VERSION:
            raise ExecutionResumeError(
                "unsupported resume format version"
            )

        if status is ResumeDecisionStatus.APPROVED:
            if not selected:
                raise ExecutionResumeError(
                    "approved decision must select at least one step"
                )
            if not isinstance(self.command, ResumeCommand):
                raise ExecutionResumeError(
                    "approved decision requires a resume command"
                )
            if self.command.selected_step_ids != selected:
                raise ExecutionResumeError(
                    "decision and command step selections differ"
                )
            if self.command.request_id != self.request_id:
                raise ExecutionResumeError(
                    "decision and command request identifiers differ"
                )
            if self.command.request_hash != self.request_hash:
                raise ExecutionResumeError(
                    "decision and command request hashes differ"
                )
            if self.command.policy_hash != self.policy_hash:
                raise ExecutionResumeError(
                    "decision and command policy hashes differ"
                )
            if self.command.checkpoint_hash != self.checkpoint_hash:
                raise ExecutionResumeError(
                    "decision and command checkpoint hashes differ"
                )
        else:
            if selected:
                raise ExecutionResumeError(
                    "rejected decision cannot select steps"
                )
            if self.command is not None:
                raise ExecutionResumeError(
                    "rejected decision cannot contain a command"
                )

        computed = self.compute_hash()
        if self.decision_hash is None:
            object.__setattr__(self, "decision_hash", computed)
        else:
            supplied = _hash(self.decision_hash, "decision_hash")
            if supplied != computed:
                raise ResumeIntegrityError(
                    "decision hash does not match decision content"
                )
            object.__setattr__(self, "decision_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "resume_decision",
            "version": self.version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_hash": self.checkpoint_hash,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "selected_step_ids": list(self.selected_step_ids),
            "issued_at": self.issued_at,
            "command": (
                self.command.to_dict()
                if self.command is not None
                else None
            ),
        }

    def compute_hash(self) -> str:
        return _sha256(self.hash_material())

    def verify_hash(self) -> None:
        if self.decision_hash != self.compute_hash():
            raise ResumeIntegrityError(
                "decision hash does not match decision content"
            )
        if self.command is not None:
            self.command.verify_hash()

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["decision_hash"] = self.decision_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeDecision":
        if data.get("record_type") != "resume_decision":
            raise ExecutionResumeError(
                "record_type must be resume_decision"
            )
        if "decision_hash" not in data:
            raise ResumeIntegrityError(
                "serialized decision is missing decision_hash"
            )
        raw_command = data.get("command")
        command = (
            ResumeCommand.from_dict(raw_command)
            if isinstance(raw_command, Mapping)
            else None
        )
        return cls(
            decision_id=data["decision_id"],
            request_id=data["request_id"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            checkpoint_id=data["checkpoint_id"],
            checkpoint_hash=data["checkpoint_hash"],
            status=ResumeDecisionStatus(data["status"]),
            reasons=tuple(data["reasons"]),
            selected_step_ids=tuple(data["selected_step_ids"]),
            issued_at=data["issued_at"],
            command=command,
            decision_hash=data["decision_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ResumeDecision":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionResumeError(
                "resume decision JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionResumeError(
                "resume decision JSON must be an object"
            )
        return cls.from_dict(data)


def decide_resume(
    request: ResumeRequest,
    checkpoint: ExecutionCheckpoint,
    assessment: ResumeAssessment,
    policy: ResumePolicy,
    *,
    issued_at: str | datetime,
) -> ResumeDecision:
    """Return a declarative authorization or rejection.

    The function never executes a step, mutates a plan, writes a checkpoint,
    or changes the journal.
    """

    if not isinstance(request, ResumeRequest):
        raise ExecutionResumeError(
            "request must be a ResumeRequest"
        )
    if not isinstance(checkpoint, ExecutionCheckpoint):
        raise ExecutionResumeError(
            "checkpoint must be an ExecutionCheckpoint"
        )
    if not isinstance(assessment, ResumeAssessment):
        raise ExecutionResumeError(
            "assessment must be a ResumeAssessment"
        )
    if not isinstance(policy, ResumePolicy):
        raise ExecutionResumeError(
            "policy must be a ResumePolicy"
        )

    normalized_issued_at = _utc_timestamp(issued_at, "issued_at")
    request_hash = request.request_hash
    policy_hash = policy.policy_hash
    checkpoint_hash = checkpoint.checkpoint_hash
    assert checkpoint_hash is not None

    reasons: list[str] = []

    try:
        checkpoint.verify_hash()
    except Exception:
        reasons.append("checkpoint integrity validation failed")

    if request.checkpoint_id != checkpoint.checkpoint_id:
        reasons.append("request checkpoint identifier does not match")
    if request.checkpoint_hash != checkpoint_hash:
        reasons.append("request checkpoint hash does not match")
    if request.plan_id != checkpoint.plan_id:
        reasons.append("request plan identifier does not match")
    if assessment.checkpoint_id != checkpoint.checkpoint_id:
        reasons.append("assessment checkpoint identifier does not match")

    if checkpoint.checkpoint_status is CheckpointStatus.BLOCKED:
        reasons.append("checkpoint is blocked")
    elif checkpoint.checkpoint_status is CheckpointStatus.TERMINAL:
        reasons.append("checkpoint is terminal")

    if (
        assessment.status is not ResumeAssessmentStatus.READY
        or not assessment.can_resume
    ):
        reasons.append(
            f"resume assessment is {assessment.status.value}"
        )

    if assessment.current_event_count != checkpoint.journal_event_count:
        reasons.append(
            "assessment event count differs from checkpoint"
        )
    if assessment.current_head_hash != checkpoint.journal_head_hash:
        reasons.append(
            "assessment journal head differs from checkpoint"
        )

    state_by_id = {
        state.step_id: state
        for state in checkpoint.step_states
    }
    assessment_ready = tuple(sorted(set(assessment.ready_step_ids)))

    for step_id in assessment_ready:
        state = state_by_id.get(step_id)
        if state is None:
            reasons.append(
                f"assessment references unknown step {step_id}"
            )
            continue
        if state.status not in {
            StepStatus.PENDING,
            StepStatus.APPROVED,
        }:
            reasons.append(
                f"assessment marks non-ready checkpoint step {step_id} as ready"
            )

    candidates = assessment_ready

    if policy.allowed_step_ids:
        allowed = set(policy.allowed_step_ids)
        candidates = tuple(
            step_id
            for step_id in candidates
            if step_id in allowed
        )

    if request.requested_step_ids:
        candidate_set = set(candidates)
        unavailable = tuple(
            step_id
            for step_id in request.requested_step_ids
            if step_id not in candidate_set
        )
        if unavailable:
            reasons.append(
                "requested steps are not resumable: "
                + ", ".join(unavailable)
            )
        selected = tuple(
            step_id
            for step_id in request.requested_step_ids
            if step_id in candidate_set
        )
    else:
        selected = candidates

    if policy.max_steps is not None:
        selected = selected[: policy.max_steps]

    if not selected:
        reasons.append("no resumable step remains after policy evaluation")

    decision_id = f"decision:{request.request_id}"

    if reasons:
        return ResumeDecision(
            decision_id=decision_id,
            request_id=request.request_id,
            request_hash=request_hash,
            policy_id=policy.policy_id,
            policy_hash=policy_hash,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            status=ResumeDecisionStatus.REJECTED,
            reasons=tuple(reasons),
            selected_step_ids=(),
            issued_at=normalized_issued_at,
            command=None,
        )

    command = ResumeCommand(
        command_id=f"command:{request.request_id}",
        request_id=request.request_id,
        request_hash=request_hash,
        policy_id=policy.policy_id,
        policy_hash=policy_hash,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint_hash,
        plan_id=checkpoint.plan_id,
        approval_reference=request.approval_reference,
        selected_step_ids=selected,
        issued_at=normalized_issued_at,
    )

    return ResumeDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        request_hash=request_hash,
        policy_id=policy.policy_id,
        policy_hash=policy_hash,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint_hash,
        status=ResumeDecisionStatus.APPROVED,
        reasons=("resume request satisfies checkpoint and policy constraints",),
        selected_step_ids=selected,
        issued_at=normalized_issued_at,
        command=command,
    )
