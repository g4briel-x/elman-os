"""Bridge artifact transaction lifecycle results into orchestration state.

The adapter integrates one cryptographically verified artifact lifecycle result
with an existing execution plan, append-only execution journal, and execution
checkpoint. It never performs filesystem transaction writes itself. Low-level
artifact mutation remains exclusively owned by the transaction lifecycle
components.

Integration is copy-on-write:

* the source execution plan is immutable;
* the source journal is cloned before new events are appended;
* the source checkpoint is never modified;
* the result embeds a new plan, journal JSONL, and checkpoint;
* repeated integration of an already journaled lifecycle is a verified no-op.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_transaction_lifecycle import (
    ArtifactTransactionLifecycleRequest,
    ArtifactTransactionLifecycleResult,
    ArtifactTransactionLifecycleRoute,
    ArtifactTransactionLifecycleState,
)
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
    ExecutionStep,
    PlanStatus,
    StepStatus,
)


ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactTransactionOrchestrationError(RuntimeError):
    """An orchestration adapter contract or state transition is invalid."""


class ArtifactTransactionOrchestrationIntegrityError(
    ArtifactTransactionOrchestrationError
):
    """An adapter request, record, or result fails integrity validation."""


class ArtifactTransactionOrchestrationStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOOP = "noop"


class ArtifactTransactionOrchestrationDecision(StrEnum):
    COMPLETE_STEP = "complete-step"
    BLOCK_STEP = "block-step"
    FAIL_STEP = "fail-step"
    NOOP = "noop"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ArtifactTransactionOrchestrationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactTransactionOrchestrationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactTransactionOrchestrationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactTransactionOrchestrationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactTransactionOrchestrationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactTransactionOrchestrationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactTransactionOrchestrationError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _plan_state_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(
        plan.to_json().encode("utf-8")
    ).hexdigest()


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_document(dict(payload))


def _validate_checkpoint_boundary(
    checkpoint: ExecutionCheckpoint,
    plan: ExecutionPlan,
    journal: ExecutionJournal,
) -> None:
    checkpoint.verify_hash()
    assessment = checkpoint.assess_resume(plan, journal)
    if assessment.status in {
        ResumeAssessmentStatus.STALE,
        ResumeAssessmentStatus.INCOMPATIBLE,
    }:
        raise ArtifactTransactionOrchestrationError(
            "checkpoint is stale or incompatible with plan and journal"
        )
    seal = journal.seal()
    checkpoint_hash = checkpoint.checkpoint_hash
    if checkpoint_hash is None:
        raise ArtifactTransactionOrchestrationIntegrityError(
            "checkpoint hash is unavailable"
        )
    if checkpoint.plan_id != plan.plan_id:
        raise ArtifactTransactionOrchestrationError(
            "checkpoint plan_id does not match execution plan"
        )
    if checkpoint.project_id != plan.project_id:
        raise ArtifactTransactionOrchestrationError(
            "checkpoint project_id does not match execution plan"
        )
    if checkpoint.plan_state_hash != _plan_state_hash(plan):
        raise ArtifactTransactionOrchestrationError(
            "checkpoint plan state hash does not match execution plan"
        )
    if (
        checkpoint.journal_event_count != seal.event_count
        or checkpoint.journal_head_hash != seal.head_hash
        or checkpoint.journal_hash != seal.journal_hash
    ):
        raise ArtifactTransactionOrchestrationError(
            "checkpoint journal boundary does not match execution journal"
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOrchestrationPolicy:
    policy_id: str
    require_running_step: bool = True
    complete_plan_when_all_steps_complete: bool = True
    block_recovered_state: bool = True
    block_conflicted_state: bool = True
    block_deferred_state: bool = True
    max_journal_reason_chars: int = 512
    version: int = ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "require_running_step",
            "complete_plan_when_all_steps_complete",
            "block_recovered_state",
            "block_conflicted_state",
            "block_deferred_state",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "max_journal_reason_chars",
            _positive_int(
                self.max_journal_reason_chars,
                "max_journal_reason_chars",
            ),
        )
        if self.version != ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION:
            raise ArtifactTransactionOrchestrationError(
                "unsupported orchestration adapter format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_orchestration_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "require_running_step": self.require_running_step,
            "complete_plan_when_all_steps_complete": (
                self.complete_plan_when_all_steps_complete
            ),
            "block_recovered_state": self.block_recovered_state,
            "block_conflicted_state": self.block_conflicted_state,
            "block_deferred_state": self.block_deferred_state,
            "max_journal_reason_chars": self.max_journal_reason_chars,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionOrchestrationPolicy":
        if (
            data.get("record_type")
            != "artifact_transaction_orchestration_policy"
        ):
            raise ArtifactTransactionOrchestrationError(
                "record_type must be artifact_transaction_orchestration_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            require_running_step=data["require_running_step"],
            complete_plan_when_all_steps_complete=data[
                "complete_plan_when_all_steps_complete"
            ],
            block_recovered_state=data["block_recovered_state"],
            block_conflicted_state=data["block_conflicted_state"],
            block_deferred_state=data["block_deferred_state"],
            max_journal_reason_chars=data[
                "max_journal_reason_chars"
            ],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionOrchestrationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionOrchestrationError(
                "orchestration policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionOrchestrationError(
                "orchestration policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOrchestrationRequest:
    orchestration_id: str
    policy_id: str
    policy_hash: str
    lifecycle_id: str
    lifecycle_request_hash: str
    lifecycle_result_hash: str
    lifecycle_final_state: ArtifactTransactionLifecycleState
    lifecycle_route: ArtifactTransactionLifecycleRoute
    transaction_id: str
    plan_id: str
    project_id: str
    step_id: str
    agent_id: str
    source_plan_state_hash: str
    source_plan_status: PlanStatus
    source_step_status: StepStatus
    source_journal_event_count: int
    source_journal_head_hash: str
    source_journal_hash: str
    source_checkpoint_id: str
    source_checkpoint_hash: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "orchestration_id",
            "policy_id",
            "lifecycle_id",
            "transaction_id",
            "plan_id",
            "project_id",
            "source_checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "policy_hash",
            "lifecycle_request_hash",
            "lifecycle_result_hash",
            "source_plan_state_hash",
            "source_journal_head_hash",
            "source_journal_hash",
            "source_checkpoint_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        try:
            final_state = ArtifactTransactionLifecycleState(
                self.lifecycle_final_state
            )
            route = ArtifactTransactionLifecycleRoute(
                self.lifecycle_route
            )
            plan_status = PlanStatus(self.source_plan_status)
            step_status = StepStatus(self.source_step_status)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionOrchestrationError(
                "request lifecycle or execution status is invalid"
            ) from exc
        object.__setattr__(
            self,
            "lifecycle_final_state",
            final_state,
        )
        object.__setattr__(self, "lifecycle_route", route)
        object.__setattr__(self, "source_plan_status", plan_status)
        object.__setattr__(self, "source_step_status", step_status)
        object.__setattr__(
            self,
            "step_id",
            _identifier(self.step_id, "step_id", _STEP_ID),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "source_journal_event_count",
            _non_negative_int(
                self.source_journal_event_count,
                "source_journal_event_count",
            ),
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "requested_at",
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        if self.version != ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION:
            raise ArtifactTransactionOrchestrationError(
                "unsupported orchestration adapter format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactTransactionOrchestrationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        lifecycle_request: ArtifactTransactionLifecycleRequest,
        lifecycle_result: ArtifactTransactionLifecycleResult,
        execution_plan: ExecutionPlan,
        execution_journal: ExecutionJournal,
        execution_checkpoint: ExecutionCheckpoint,
        policy: ArtifactTransactionOrchestrationPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        orchestration_id: str | None = None,
    ) -> "ArtifactTransactionOrchestrationRequest":
        if not isinstance(
            lifecycle_request,
            ArtifactTransactionLifecycleRequest,
        ):
            raise ArtifactTransactionOrchestrationError(
                "lifecycle_request must be an ArtifactTransactionLifecycleRequest"
            )
        if not isinstance(
            lifecycle_result,
            ArtifactTransactionLifecycleResult,
        ):
            raise ArtifactTransactionOrchestrationError(
                "lifecycle_result must be an ArtifactTransactionLifecycleResult"
            )
        if not isinstance(execution_plan, ExecutionPlan):
            raise ArtifactTransactionOrchestrationError(
                "execution_plan must be an ExecutionPlan"
            )
        if not isinstance(execution_journal, ExecutionJournal):
            raise ArtifactTransactionOrchestrationError(
                "execution_journal must be an ExecutionJournal"
            )
        if not isinstance(execution_checkpoint, ExecutionCheckpoint):
            raise ArtifactTransactionOrchestrationError(
                "execution_checkpoint must be an ExecutionCheckpoint"
            )
        if not isinstance(policy, ArtifactTransactionOrchestrationPolicy):
            raise ArtifactTransactionOrchestrationError(
                "policy must be an ArtifactTransactionOrchestrationPolicy"
            )
        lifecycle_request.verify_hash()
        lifecycle_result.verify_hash()
        _validate_checkpoint_boundary(
            execution_checkpoint,
            execution_plan,
            execution_journal,
        )
        lifecycle_request_hash = lifecycle_request.request_hash
        lifecycle_result_hash = lifecycle_result.result_hash
        checkpoint_hash = execution_checkpoint.checkpoint_hash
        assert lifecycle_request_hash is not None
        assert lifecycle_result_hash is not None
        assert checkpoint_hash is not None
        if lifecycle_result.request_hash != lifecycle_request_hash:
            raise ArtifactTransactionOrchestrationError(
                "lifecycle result does not match lifecycle request"
            )
        if lifecycle_result.lifecycle_id != lifecycle_request.lifecycle_id:
            raise ArtifactTransactionOrchestrationError(
                "lifecycle identifiers do not match"
            )
        if lifecycle_result.transaction_id != lifecycle_request.transaction_id:
            raise ArtifactTransactionOrchestrationError(
                "transaction identifiers do not match"
            )
        if lifecycle_request.plan_id != execution_plan.plan_id:
            raise ArtifactTransactionOrchestrationError(
                "lifecycle plan_id does not match execution plan"
            )
        target_step = execution_plan.get_step(
            lifecycle_request.step_id
        )
        if target_step.assigned_agent_id != lifecycle_request.agent_id:
            raise ArtifactTransactionOrchestrationError(
                "target step assigned agent does not match lifecycle agent"
            )
        seal = execution_journal.seal()
        plan_hash = _plan_state_hash(execution_plan)
        requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(requested_at, "requested_at")
        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_transaction_orchestration_identity"
                ),
                "policy_hash": policy.policy_hash,
                "lifecycle_result_hash": lifecycle_result_hash,
                "source_plan_state_hash": plan_hash,
                "source_journal_hash": seal.journal_hash,
                "source_checkpoint_hash": checkpoint_hash,
            }
        )
        effective_id = (
            orchestration_id
            if orchestration_id is not None
            else f"artifact-orchestration:{identity_hash}"
        )
        return cls(
            orchestration_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            lifecycle_id=lifecycle_request.lifecycle_id,
            lifecycle_request_hash=lifecycle_request_hash,
            lifecycle_result_hash=lifecycle_result_hash,
            lifecycle_final_state=lifecycle_result.final_state,
            lifecycle_route=lifecycle_result.route,
            transaction_id=lifecycle_result.transaction_id,
            plan_id=execution_plan.plan_id,
            project_id=execution_plan.project_id,
            step_id=lifecycle_request.step_id,
            agent_id=lifecycle_request.agent_id,
            source_plan_state_hash=plan_hash,
            source_plan_status=execution_plan.status,
            source_step_status=target_step.status,
            source_journal_event_count=seal.event_count,
            source_journal_head_hash=seal.head_hash,
            source_journal_hash=seal.journal_hash,
            source_checkpoint_id=execution_checkpoint.checkpoint_id,
            source_checkpoint_hash=checkpoint_hash,
            requested_by=requester,
            requested_at=timestamp,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_orchestration_request",
            "version": self.version,
            "orchestration_id": self.orchestration_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "lifecycle_id": self.lifecycle_id,
            "lifecycle_request_hash": self.lifecycle_request_hash,
            "lifecycle_result_hash": self.lifecycle_result_hash,
            "lifecycle_final_state": self.lifecycle_final_state.value,
            "lifecycle_route": self.lifecycle_route.value,
            "transaction_id": self.transaction_id,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "source_plan_state_hash": self.source_plan_state_hash,
            "source_plan_status": self.source_plan_status.value,
            "source_step_status": self.source_step_status.value,
            "source_journal_event_count": (
                self.source_journal_event_count
            ),
            "source_journal_head_hash": (
                self.source_journal_head_hash
            ),
            "source_journal_hash": self.source_journal_hash,
            "source_checkpoint_id": self.source_checkpoint_id,
            "source_checkpoint_hash": self.source_checkpoint_hash,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactTransactionOrchestrationIntegrityError(
                "request hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionOrchestrationRequest":
        if (
            data.get("record_type")
            != "artifact_transaction_orchestration_request"
        ):
            raise ArtifactTransactionOrchestrationError(
                "record_type must be artifact_transaction_orchestration_request"
            )
        if "request_hash" not in data:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            orchestration_id=data["orchestration_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            lifecycle_id=data["lifecycle_id"],
            lifecycle_request_hash=data["lifecycle_request_hash"],
            lifecycle_result_hash=data["lifecycle_result_hash"],
            lifecycle_final_state=ArtifactTransactionLifecycleState(
                data["lifecycle_final_state"]
            ),
            lifecycle_route=ArtifactTransactionLifecycleRoute(
                data["lifecycle_route"]
            ),
            transaction_id=data["transaction_id"],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            source_plan_state_hash=data["source_plan_state_hash"],
            source_plan_status=PlanStatus(
                data["source_plan_status"]
            ),
            source_step_status=StepStatus(
                data["source_step_status"]
            ),
            source_journal_event_count=data[
                "source_journal_event_count"
            ],
            source_journal_head_hash=data[
                "source_journal_head_hash"
            ],
            source_journal_hash=data["source_journal_hash"],
            source_checkpoint_id=data["source_checkpoint_id"],
            source_checkpoint_hash=data["source_checkpoint_hash"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionOrchestrationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionOrchestrationError(
                "orchestration request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionOrchestrationError(
                "orchestration request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOrchestrationRecord:
    index: int
    event_sequence: int
    event_type: ExecutionEventType
    step_id: str | None
    agent_id: str | None
    event_hash: str
    payload_hash: str
    reason: str
    record_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        object.__setattr__(
            self,
            "event_sequence",
            _positive_int(self.event_sequence, "event_sequence"),
        )
        try:
            event_type = ExecutionEventType(self.event_type)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionOrchestrationError(
                "event_type is invalid"
            ) from exc
        object.__setattr__(self, "event_type", event_type)
        if self.step_id is not None:
            object.__setattr__(
                self,
                "step_id",
                _identifier(self.step_id, "step_id", _STEP_ID),
            )
        if self.agent_id is not None:
            object.__setattr__(
                self,
                "agent_id",
                _identifier(self.agent_id, "agent_id", _AGENT_ID),
            )
        object.__setattr__(
            self,
            "event_hash",
            _hash(self.event_hash, "event_hash"),
        )
        object.__setattr__(
            self,
            "payload_hash",
            _hash(self.payload_hash, "payload_hash"),
        )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise ArtifactTransactionOrchestrationIntegrityError(
                    "record hash does not match record content"
                )
            object.__setattr__(self, "record_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "event_sequence": self.event_sequence,
            "event_type": self.event_type.value,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "event_hash": self.event_hash,
            "payload_hash": self.payload_hash,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise ArtifactTransactionOrchestrationIntegrityError(
                "record hash does not match record content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionOrchestrationRecord":
        return cls(
            index=data["index"],
            event_sequence=data["event_sequence"],
            event_type=ExecutionEventType(data["event_type"]),
            step_id=data.get("step_id"),
            agent_id=data.get("agent_id"),
            event_hash=data["event_hash"],
            payload_hash=data["payload_hash"],
            reason=data["reason"],
            record_hash=data.get("record_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOrchestrationResult:
    orchestration_id: str
    status: ArtifactTransactionOrchestrationStatus
    decision: ArtifactTransactionOrchestrationDecision
    request_hash: str
    policy_id: str
    policy_hash: str
    lifecycle_id: str
    lifecycle_result_hash: str
    lifecycle_final_state: ArtifactTransactionLifecycleState
    lifecycle_route: ArtifactTransactionLifecycleRoute
    transaction_id: str
    plan_id: str
    project_id: str
    step_id: str
    agent_id: str
    source_plan_state_hash: str
    result_plan_state_hash: str
    source_plan_status: PlanStatus
    result_plan_status: PlanStatus
    source_step_status: StepStatus
    result_step_status: StepStatus
    source_journal_event_count: int
    result_journal_event_count: int
    source_journal_head_hash: str
    result_journal_head_hash: str
    source_journal_hash: str
    result_journal_hash: str
    source_checkpoint_id: str
    source_checkpoint_hash: str
    result_checkpoint_id: str
    result_checkpoint_hash: str
    records: tuple[ArtifactTransactionOrchestrationRecord, ...]
    updated_plan_json: str
    updated_journal_jsonl: str
    updated_checkpoint_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "orchestration_id",
            "policy_id",
            "lifecycle_id",
            "transaction_id",
            "plan_id",
            "project_id",
            "source_checkpoint_id",
            "result_checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        try:
            status = ArtifactTransactionOrchestrationStatus(self.status)
            decision = ArtifactTransactionOrchestrationDecision(
                self.decision
            )
            lifecycle_state = ArtifactTransactionLifecycleState(
                self.lifecycle_final_state
            )
            lifecycle_route = ArtifactTransactionLifecycleRoute(
                self.lifecycle_route
            )
            source_plan_status = PlanStatus(self.source_plan_status)
            result_plan_status = PlanStatus(self.result_plan_status)
            source_step_status = StepStatus(self.source_step_status)
            result_step_status = StepStatus(self.result_step_status)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionOrchestrationError(
                "result status, decision, lifecycle, or execution state is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(
            self,
            "lifecycle_final_state",
            lifecycle_state,
        )
        object.__setattr__(self, "lifecycle_route", lifecycle_route)
        object.__setattr__(
            self,
            "source_plan_status",
            source_plan_status,
        )
        object.__setattr__(
            self,
            "result_plan_status",
            result_plan_status,
        )
        object.__setattr__(
            self,
            "source_step_status",
            source_step_status,
        )
        object.__setattr__(
            self,
            "result_step_status",
            result_step_status,
        )
        object.__setattr__(
            self,
            "step_id",
            _identifier(self.step_id, "step_id", _STEP_ID),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        for field_name in (
            "request_hash",
            "policy_hash",
            "lifecycle_result_hash",
            "source_plan_state_hash",
            "result_plan_state_hash",
            "source_journal_head_hash",
            "result_journal_head_hash",
            "source_journal_hash",
            "result_journal_hash",
            "source_checkpoint_hash",
            "result_checkpoint_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        for field_name in (
            "source_journal_event_count",
            "result_journal_event_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        records = tuple(self.records)
        if not all(
            isinstance(
                item,
                ArtifactTransactionOrchestrationRecord,
            )
            for item in records
        ):
            raise ArtifactTransactionOrchestrationError(
                "records must contain orchestration records"
            )
        if tuple(item.index for item in records) != tuple(
            range(len(records))
        ):
            raise ArtifactTransactionOrchestrationError(
                "record indexes must be contiguous from zero"
            )
        for item in records:
            item.verify_hash()
        object.__setattr__(self, "records", records)
        if (
            self.result_journal_event_count
            - self.source_journal_event_count
            != len(records)
        ):
            raise ArtifactTransactionOrchestrationIntegrityError(
                "journal event count delta does not match records"
            )

        plan_json = _text(self.updated_plan_json, "updated_plan_json")
        journal_jsonl = _text(
            self.updated_journal_jsonl,
            "updated_journal_jsonl",
        )
        checkpoint_json = _text(
            self.updated_checkpoint_json,
            "updated_checkpoint_json",
        )
        object.__setattr__(self, "updated_plan_json", plan_json)
        object.__setattr__(
            self,
            "updated_journal_jsonl",
            journal_jsonl,
        )
        object.__setattr__(
            self,
            "updated_checkpoint_json",
            checkpoint_json,
        )
        try:
            plan = ExecutionPlan.from_json(plan_json)
            journal = ExecutionJournal.from_jsonl(journal_jsonl)
            checkpoint = ExecutionCheckpoint.from_json(checkpoint_json)
        except Exception as exc:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded execution state cannot be reconstructed"
            ) from exc
        try:
            _validate_checkpoint_boundary(checkpoint, plan, journal)
        except ArtifactTransactionOrchestrationError as exc:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded execution checkpoint boundary is invalid"
            ) from exc
        seal = journal.seal()
        checkpoint_hash = checkpoint.checkpoint_hash
        assert checkpoint_hash is not None
        if plan.plan_id != self.plan_id:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded plan_id does not match result"
            )
        if plan.project_id != self.project_id:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded project_id does not match result"
            )
        if _plan_state_hash(plan) != self.result_plan_state_hash:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded plan hash does not match result"
            )
        if plan.status is not result_plan_status:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded plan status does not match result"
            )
        if plan.get_step(self.step_id).status is not result_step_status:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded step status does not match result"
            )
        if (
            seal.event_count != self.result_journal_event_count
            or seal.head_hash != self.result_journal_head_hash
            or seal.journal_hash != self.result_journal_hash
        ):
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded journal seal does not match result"
            )
        if (
            checkpoint.checkpoint_id != self.result_checkpoint_id
            or checkpoint_hash != self.result_checkpoint_hash
        ):
            raise ArtifactTransactionOrchestrationIntegrityError(
                "embedded checkpoint does not match result"
            )
        if status is ArtifactTransactionOrchestrationStatus.NOOP:
            if records:
                raise ArtifactTransactionOrchestrationIntegrityError(
                    "noop result cannot contain new journal records"
                )
            if (
                self.source_plan_state_hash
                != self.result_plan_state_hash
                or self.source_journal_hash
                != self.result_journal_hash
                or self.source_checkpoint_hash
                != self.result_checkpoint_hash
            ):
                raise ArtifactTransactionOrchestrationIntegrityError(
                    "noop result must preserve all source state"
                )
        object.__setattr__(
            self,
            "completed_at",
            _utc_timestamp(self.completed_at, "completed_at"),
        )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        if self.version != ARTIFACT_TRANSACTION_ORCHESTRATION_FORMAT_VERSION:
            raise ArtifactTransactionOrchestrationError(
                "unsupported orchestration adapter format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactTransactionOrchestrationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def execution_plan(self) -> ExecutionPlan:
        return ExecutionPlan.from_json(self.updated_plan_json)

    @property
    def execution_journal(self) -> ExecutionJournal:
        return ExecutionJournal.from_jsonl(
            self.updated_journal_jsonl
        )

    @property
    def execution_checkpoint(self) -> ExecutionCheckpoint:
        return ExecutionCheckpoint.from_json(
            self.updated_checkpoint_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_orchestration_result",
            "version": self.version,
            "orchestration_id": self.orchestration_id,
            "status": self.status.value,
            "decision": self.decision.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "lifecycle_id": self.lifecycle_id,
            "lifecycle_result_hash": self.lifecycle_result_hash,
            "lifecycle_final_state": self.lifecycle_final_state.value,
            "lifecycle_route": self.lifecycle_route.value,
            "transaction_id": self.transaction_id,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "source_plan_state_hash": self.source_plan_state_hash,
            "result_plan_state_hash": self.result_plan_state_hash,
            "source_plan_status": self.source_plan_status.value,
            "result_plan_status": self.result_plan_status.value,
            "source_step_status": self.source_step_status.value,
            "result_step_status": self.result_step_status.value,
            "source_journal_event_count": (
                self.source_journal_event_count
            ),
            "result_journal_event_count": (
                self.result_journal_event_count
            ),
            "source_journal_head_hash": (
                self.source_journal_head_hash
            ),
            "result_journal_head_hash": (
                self.result_journal_head_hash
            ),
            "source_journal_hash": self.source_journal_hash,
            "result_journal_hash": self.result_journal_hash,
            "source_checkpoint_id": self.source_checkpoint_id,
            "source_checkpoint_hash": self.source_checkpoint_hash,
            "result_checkpoint_id": self.result_checkpoint_id,
            "result_checkpoint_hash": self.result_checkpoint_hash,
            "records": [item.to_dict() for item in self.records],
            "updated_plan_json": self.updated_plan_json,
            "updated_journal_jsonl": self.updated_journal_jsonl,
            "updated_checkpoint_json": self.updated_checkpoint_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactTransactionOrchestrationIntegrityError(
                "result hash does not match result content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["result_hash"] = self.result_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionOrchestrationResult":
        if (
            data.get("record_type")
            != "artifact_transaction_orchestration_result"
        ):
            raise ArtifactTransactionOrchestrationError(
                "record_type must be artifact_transaction_orchestration_result"
            )
        if "result_hash" not in data:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            orchestration_id=data["orchestration_id"],
            status=ArtifactTransactionOrchestrationStatus(
                data["status"]
            ),
            decision=ArtifactTransactionOrchestrationDecision(
                data["decision"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            lifecycle_id=data["lifecycle_id"],
            lifecycle_result_hash=data["lifecycle_result_hash"],
            lifecycle_final_state=ArtifactTransactionLifecycleState(
                data["lifecycle_final_state"]
            ),
            lifecycle_route=ArtifactTransactionLifecycleRoute(
                data["lifecycle_route"]
            ),
            transaction_id=data["transaction_id"],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            source_plan_state_hash=data[
                "source_plan_state_hash"
            ],
            result_plan_state_hash=data[
                "result_plan_state_hash"
            ],
            source_plan_status=PlanStatus(
                data["source_plan_status"]
            ),
            result_plan_status=PlanStatus(
                data["result_plan_status"]
            ),
            source_step_status=StepStatus(
                data["source_step_status"]
            ),
            result_step_status=StepStatus(
                data["result_step_status"]
            ),
            source_journal_event_count=data[
                "source_journal_event_count"
            ],
            result_journal_event_count=data[
                "result_journal_event_count"
            ],
            source_journal_head_hash=data[
                "source_journal_head_hash"
            ],
            result_journal_head_hash=data[
                "result_journal_head_hash"
            ],
            source_journal_hash=data["source_journal_hash"],
            result_journal_hash=data["result_journal_hash"],
            source_checkpoint_id=data["source_checkpoint_id"],
            source_checkpoint_hash=data["source_checkpoint_hash"],
            result_checkpoint_id=data["result_checkpoint_id"],
            result_checkpoint_hash=data["result_checkpoint_hash"],
            records=tuple(
                ArtifactTransactionOrchestrationRecord.from_dict(item)
                for item in data["records"]
            ),
            updated_plan_json=data["updated_plan_json"],
            updated_journal_jsonl=data["updated_journal_jsonl"],
            updated_checkpoint_json=data["updated_checkpoint_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionOrchestrationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionOrchestrationError(
                "orchestration result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionOrchestrationError(
                "orchestration result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOrchestrationAdapter:
    request: ArtifactTransactionOrchestrationRequest
    lifecycle_request: ArtifactTransactionLifecycleRequest
    lifecycle_result: ArtifactTransactionLifecycleResult
    execution_plan: ExecutionPlan
    execution_journal: ExecutionJournal
    execution_checkpoint: ExecutionCheckpoint
    policy: ArtifactTransactionOrchestrationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactTransactionOrchestrationRequest,
        ):
            raise ArtifactTransactionOrchestrationError(
                "request must be an ArtifactTransactionOrchestrationRequest"
            )
        if not isinstance(
            self.lifecycle_request,
            ArtifactTransactionLifecycleRequest,
        ):
            raise ArtifactTransactionOrchestrationError(
                "lifecycle_request must be an ArtifactTransactionLifecycleRequest"
            )
        if not isinstance(
            self.lifecycle_result,
            ArtifactTransactionLifecycleResult,
        ):
            raise ArtifactTransactionOrchestrationError(
                "lifecycle_result must be an ArtifactTransactionLifecycleResult"
            )
        if not isinstance(self.execution_plan, ExecutionPlan):
            raise ArtifactTransactionOrchestrationError(
                "execution_plan must be an ExecutionPlan"
            )
        if not isinstance(self.execution_journal, ExecutionJournal):
            raise ArtifactTransactionOrchestrationError(
                "execution_journal must be an ExecutionJournal"
            )
        if not isinstance(
            self.execution_checkpoint,
            ExecutionCheckpoint,
        ):
            raise ArtifactTransactionOrchestrationError(
                "execution_checkpoint must be an ExecutionCheckpoint"
            )
        if not isinstance(
            self.policy,
            ArtifactTransactionOrchestrationPolicy,
        ):
            raise ArtifactTransactionOrchestrationError(
                "policy must be an ArtifactTransactionOrchestrationPolicy"
            )
        self.request.verify_hash()
        self.lifecycle_request.verify_hash()
        self.lifecycle_result.verify_hash()
        _validate_checkpoint_boundary(
            self.execution_checkpoint,
            self.execution_plan,
            self.execution_journal,
        )
        lifecycle_request_hash = self.lifecycle_request.request_hash
        lifecycle_result_hash = self.lifecycle_result.result_hash
        checkpoint_hash = self.execution_checkpoint.checkpoint_hash
        assert lifecycle_request_hash is not None
        assert lifecycle_result_hash is not None
        assert checkpoint_hash is not None
        seal = self.execution_journal.seal()
        target_step = self.execution_plan.get_step(
            self.lifecycle_request.step_id
        )
        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "lifecycle_id": self.lifecycle_request.lifecycle_id,
            "lifecycle_request_hash": lifecycle_request_hash,
            "lifecycle_result_hash": lifecycle_result_hash,
            "lifecycle_final_state": self.lifecycle_result.final_state,
            "lifecycle_route": self.lifecycle_result.route,
            "transaction_id": self.lifecycle_result.transaction_id,
            "plan_id": self.execution_plan.plan_id,
            "project_id": self.execution_plan.project_id,
            "step_id": self.lifecycle_request.step_id,
            "agent_id": self.lifecycle_request.agent_id,
            "source_plan_state_hash": _plan_state_hash(
                self.execution_plan
            ),
            "source_plan_status": self.execution_plan.status,
            "source_step_status": target_step.status,
            "source_journal_event_count": seal.event_count,
            "source_journal_head_hash": seal.head_hash,
            "source_journal_hash": seal.journal_hash,
            "source_checkpoint_id": (
                self.execution_checkpoint.checkpoint_id
            ),
            "source_checkpoint_hash": checkpoint_hash,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactTransactionOrchestrationError(
                    f"request {field_name} does not match adapter source"
                )
        if self.lifecycle_result.request_hash != lifecycle_request_hash:
            raise ArtifactTransactionOrchestrationError(
                "lifecycle result does not match lifecycle request"
            )
        if self.lifecycle_request.plan_id != self.execution_plan.plan_id:
            raise ArtifactTransactionOrchestrationError(
                "lifecycle plan does not match execution plan"
            )
        if (
            target_step.assigned_agent_id
            != self.lifecycle_request.agent_id
        ):
            raise ArtifactTransactionOrchestrationError(
                "target step assigned agent does not match lifecycle agent"
            )

    def integrate(self) -> ArtifactTransactionOrchestrationResult:
        existing_event = self._existing_lifecycle_event()
        if existing_event is not None:
            return self._noop(existing_event)

        source_step = self.execution_plan.get_step(
            self.request.step_id
        )
        if (
            self.policy.require_running_step
            and source_step.status is not StepStatus.RUNNING
        ):
            raise ArtifactTransactionOrchestrationError(
                "target step must be running before lifecycle integration"
            )
        if self.execution_plan.status is not PlanStatus.RUNNING:
            raise ArtifactTransactionOrchestrationError(
                "execution plan must be running before lifecycle integration"
            )

        decision, result_step_status = self._decision()
        updated_plan = self._updated_plan(
            source_step,
            result_step_status,
        )
        updated_journal = ExecutionJournal.from_events(
            self.execution_journal.plan_id,
            self.execution_journal.events,
        )
        payload = self._journal_payload(
            decision,
            result_step_status,
        )
        new_events: list[ExecutionEvent] = []

        step_event_type = {
            StepStatus.COMPLETED: ExecutionEventType.STEP_COMPLETED,
            StepStatus.BLOCKED: ExecutionEventType.STEP_BLOCKED,
            StepStatus.FAILED: ExecutionEventType.STEP_FAILED,
        }[result_step_status]
        step_event = updated_journal.append(
            step_event_type,
            self.request.requested_at,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            payload=payload,
        )
        new_events.append(step_event)

        if updated_plan.status is not self.execution_plan.status:
            plan_event_type = {
                PlanStatus.COMPLETED: ExecutionEventType.PLAN_COMPLETED,
                PlanStatus.BLOCKED: ExecutionEventType.PLAN_BLOCKED,
                PlanStatus.FAILED: ExecutionEventType.PLAN_FAILED,
            }[updated_plan.status]
            plan_event = updated_journal.append(
                plan_event_type,
                self.request.requested_at,
                payload={
                    **payload,
                    "affected_step_id": self.request.step_id,
                },
            )
            new_events.append(plan_event)

        checkpoint_id = self._checkpoint_id(
            updated_plan,
            updated_journal,
        )
        updated_checkpoint = ExecutionCheckpoint.capture(
            updated_plan,
            updated_journal,
            checkpoint_id=checkpoint_id,
            created_at=self.request.requested_at,
        )
        records = tuple(
            self._record(index, event)
            for index, event in enumerate(new_events)
        )
        result_status = {
            ArtifactTransactionOrchestrationDecision.COMPLETE_STEP: (
                ArtifactTransactionOrchestrationStatus.COMPLETED
            ),
            ArtifactTransactionOrchestrationDecision.BLOCK_STEP: (
                ArtifactTransactionOrchestrationStatus.BLOCKED
            ),
            ArtifactTransactionOrchestrationDecision.FAIL_STEP: (
                ArtifactTransactionOrchestrationStatus.FAILED
            ),
        }[decision]
        return self._result(
            status=result_status,
            decision=decision,
            updated_plan=updated_plan,
            updated_journal=updated_journal,
            updated_checkpoint=updated_checkpoint,
            records=records,
            reason=self._result_reason(decision),
        )

    def _decision(
        self,
    ) -> tuple[
        ArtifactTransactionOrchestrationDecision,
        StepStatus,
    ]:
        state = self.lifecycle_result.final_state
        if state is ArtifactTransactionLifecycleState.COMMITTED:
            return (
                ArtifactTransactionOrchestrationDecision.COMPLETE_STEP,
                StepStatus.COMPLETED,
            )
        if state is ArtifactTransactionLifecycleState.RECOVERED:
            return (
                (
                    ArtifactTransactionOrchestrationDecision.BLOCK_STEP
                    if self.policy.block_recovered_state
                    else ArtifactTransactionOrchestrationDecision.FAIL_STEP
                ),
                (
                    StepStatus.BLOCKED
                    if self.policy.block_recovered_state
                    else StepStatus.FAILED
                ),
            )
        if state is ArtifactTransactionLifecycleState.CONFLICTED:
            return (
                (
                    ArtifactTransactionOrchestrationDecision.BLOCK_STEP
                    if self.policy.block_conflicted_state
                    else ArtifactTransactionOrchestrationDecision.FAIL_STEP
                ),
                (
                    StepStatus.BLOCKED
                    if self.policy.block_conflicted_state
                    else StepStatus.FAILED
                ),
            )
        if state in {
            ArtifactTransactionLifecycleState.CLEAN,
            ArtifactTransactionLifecycleState.APPLY_REQUIRED,
            ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
        }:
            return (
                (
                    ArtifactTransactionOrchestrationDecision.BLOCK_STEP
                    if self.policy.block_deferred_state
                    else ArtifactTransactionOrchestrationDecision.FAIL_STEP
                ),
                (
                    StepStatus.BLOCKED
                    if self.policy.block_deferred_state
                    else StepStatus.FAILED
                ),
            )
        return (
            ArtifactTransactionOrchestrationDecision.FAIL_STEP,
            StepStatus.FAILED,
        )

    def _updated_plan(
        self,
        source_step: ExecutionStep,
        result_step_status: StepStatus,
    ) -> ExecutionPlan:
        updated_step = replace(
            source_step,
            status=result_step_status,
        )
        updated_steps = tuple(
            updated_step
            if step.step_id == updated_step.step_id
            else step
            for step in self.execution_plan.steps
        )
        if result_step_status is StepStatus.COMPLETED:
            all_completed = all(
                step.status is StepStatus.COMPLETED
                for step in updated_steps
            )
            result_plan_status = (
                PlanStatus.COMPLETED
                if (
                    all_completed
                    and self.policy.complete_plan_when_all_steps_complete
                )
                else PlanStatus.RUNNING
            )
        elif result_step_status is StepStatus.BLOCKED:
            result_plan_status = PlanStatus.BLOCKED
        else:
            result_plan_status = PlanStatus.FAILED
        return replace(
            self.execution_plan,
            steps=updated_steps,
            status=result_plan_status,
        )

    def _journal_payload(
        self,
        decision: ArtifactTransactionOrchestrationDecision,
        result_step_status: StepStatus,
    ) -> dict[str, Any]:
        lifecycle_hash = self.lifecycle_result.result_hash
        request_hash = self.request.request_hash
        assert lifecycle_hash is not None
        assert request_hash is not None
        reason = self.lifecycle_result.reason[
            : self.policy.max_journal_reason_chars
        ]
        return {
            "artifact_orchestration_id": (
                self.request.orchestration_id
            ),
            "artifact_orchestration_request_hash": request_hash,
            "artifact_lifecycle_id": self.lifecycle_result.lifecycle_id,
            "artifact_lifecycle_result_hash": lifecycle_hash,
            "artifact_transaction_id": (
                self.lifecycle_result.transaction_id
            ),
            "artifact_lifecycle_final_state": (
                self.lifecycle_result.final_state.value
            ),
            "artifact_lifecycle_route": (
                self.lifecycle_result.route.value
            ),
            "artifact_orchestration_decision": decision.value,
            "result_step_status": result_step_status.value,
            "lifecycle_reason": reason,
        }

    def _checkpoint_id(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> str:
        seal = journal.seal()
        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_transaction_orchestration_checkpoint_identity"
                ),
                "orchestration_id": self.request.orchestration_id,
                "result_plan_state_hash": _plan_state_hash(plan),
                "result_journal_hash": seal.journal_hash,
            }
        )
        return f"artifact-checkpoint:{identity_hash}"

    def _record(
        self,
        index: int,
        event: ExecutionEvent,
    ) -> ArtifactTransactionOrchestrationRecord:
        event_hash = event.event_hash
        assert event_hash is not None
        return ArtifactTransactionOrchestrationRecord(
            index=index,
            event_sequence=event.sequence,
            event_type=event.event_type,
            step_id=event.step_id,
            agent_id=event.agent_id,
            event_hash=event_hash,
            payload_hash=_payload_hash(event.payload),
            reason=(
                "APPENDED: lifecycle result was propagated into the "
                f"{event.event_type.value} journal event"
            ),
        )

    def _existing_lifecycle_event(
        self,
    ) -> ExecutionEvent | None:
        lifecycle_hash = self.lifecycle_result.result_hash
        assert lifecycle_hash is not None
        matches: list[ExecutionEvent] = []
        for event in self.execution_journal.events:
            payload_hash = event.payload.get(
                "artifact_lifecycle_result_hash"
            )
            if payload_hash == lifecycle_hash:
                matches.append(event)
        if not matches:
            return None
        if any(
            event.step_id not in {None, self.request.step_id}
            for event in matches
        ):
            raise ArtifactTransactionOrchestrationIntegrityError(
                "lifecycle result is journaled against another step"
            )
        step_events = [
            event
            for event in matches
            if event.step_id == self.request.step_id
        ]
        if len(step_events) != 1:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "lifecycle result has an invalid journal integration count"
            )
        event = step_events[0]
        if event.agent_id != self.request.agent_id:
            raise ArtifactTransactionOrchestrationIntegrityError(
                "journaled lifecycle event agent does not match request"
            )
        return event

    def _noop(
        self,
        existing_event: ExecutionEvent,
    ) -> ArtifactTransactionOrchestrationResult:
        return self._result(
            status=ArtifactTransactionOrchestrationStatus.NOOP,
            decision=ArtifactTransactionOrchestrationDecision.NOOP,
            updated_plan=self.execution_plan,
            updated_journal=self.execution_journal,
            updated_checkpoint=self.execution_checkpoint,
            records=(),
            reason=(
                "NOOP: lifecycle result is already present in journal "
                f"event {existing_event.sequence}"
            ),
        )

    def _result_reason(
        self,
        decision: ArtifactTransactionOrchestrationDecision,
    ) -> str:
        mapping = {
            ArtifactTransactionOrchestrationDecision.COMPLETE_STEP: (
                "COMPLETED: committed artifact lifecycle completed the "
                "execution step"
            ),
            ArtifactTransactionOrchestrationDecision.BLOCK_STEP: (
                "BLOCKED: artifact lifecycle requires controlled "
                "orchestration follow-up"
            ),
            ArtifactTransactionOrchestrationDecision.FAIL_STEP: (
                "FAILED: artifact lifecycle produced a terminal "
                "orchestration failure"
            ),
        }
        return mapping[decision]

    def _result(
        self,
        *,
        status: ArtifactTransactionOrchestrationStatus,
        decision: ArtifactTransactionOrchestrationDecision,
        updated_plan: ExecutionPlan,
        updated_journal: ExecutionJournal,
        updated_checkpoint: ExecutionCheckpoint,
        records: tuple[ArtifactTransactionOrchestrationRecord, ...],
        reason: str,
    ) -> ArtifactTransactionOrchestrationResult:
        request_hash = self.request.request_hash
        lifecycle_hash = self.lifecycle_result.result_hash
        source_checkpoint_hash = self.execution_checkpoint.checkpoint_hash
        result_checkpoint_hash = updated_checkpoint.checkpoint_hash
        assert request_hash is not None
        assert lifecycle_hash is not None
        assert source_checkpoint_hash is not None
        assert result_checkpoint_hash is not None
        source_seal = self.execution_journal.seal()
        result_seal = updated_journal.seal()
        return ArtifactTransactionOrchestrationResult(
            orchestration_id=self.request.orchestration_id,
            status=status,
            decision=decision,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            lifecycle_id=self.lifecycle_result.lifecycle_id,
            lifecycle_result_hash=lifecycle_hash,
            lifecycle_final_state=self.lifecycle_result.final_state,
            lifecycle_route=self.lifecycle_result.route,
            transaction_id=self.lifecycle_result.transaction_id,
            plan_id=self.execution_plan.plan_id,
            project_id=self.execution_plan.project_id,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            source_plan_state_hash=_plan_state_hash(
                self.execution_plan
            ),
            result_plan_state_hash=_plan_state_hash(updated_plan),
            source_plan_status=self.execution_plan.status,
            result_plan_status=updated_plan.status,
            source_step_status=self.execution_plan.get_step(
                self.request.step_id
            ).status,
            result_step_status=updated_plan.get_step(
                self.request.step_id
            ).status,
            source_journal_event_count=source_seal.event_count,
            result_journal_event_count=result_seal.event_count,
            source_journal_head_hash=source_seal.head_hash,
            result_journal_head_hash=result_seal.head_hash,
            source_journal_hash=source_seal.journal_hash,
            result_journal_hash=result_seal.journal_hash,
            source_checkpoint_id=(
                self.execution_checkpoint.checkpoint_id
            ),
            source_checkpoint_hash=source_checkpoint_hash,
            result_checkpoint_id=updated_checkpoint.checkpoint_id,
            result_checkpoint_hash=result_checkpoint_hash,
            records=records,
            updated_plan_json=updated_plan.to_json(),
            updated_journal_jsonl=updated_journal.to_jsonl(),
            updated_checkpoint_json=updated_checkpoint.to_json(),
            completed_at=self.request.requested_at,
            reason=reason,
        )
