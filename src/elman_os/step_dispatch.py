"""Deterministic preparation of one agent step dispatch for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .agent_contracts import (
    AgentRequest,
    AgentRegistry,
    UnknownAgentError,
    canonical_json,
)
from .execution_checkpoint import ExecutionCheckpoint
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
from .resume_application import ResumeApplicationResult


STEP_DISPATCH_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_MARKER_DISPATCH_ID = "step_dispatch_id"
_MARKER_REQUEST_HASH = "step_dispatch_request_hash"
_MARKER_RESUME_APPLICATION_ID = "resume_application_id"
_MARKER_RESUME_RESULT_HASH = "resume_application_result_hash"
_MARKER_COMMAND_ID = "resume_command_id"
_MARKER_COMMAND_HASH = "resume_command_hash"
_MARKER_STEP_ID = "step_dispatch_step_id"
_MARKER_AGENT_ID = "step_dispatch_agent_id"


class StepDispatchError(ValueError):
    """A dispatch request or state transition is invalid."""


class StepDispatchIntegrityError(StepDispatchError):
    """A dispatch request, marker, or result fails integrity validation."""


class StepDispatchConflictError(StepDispatchError):
    """A dispatch identifier, agent assignment, or journal marker conflicts."""


class StepDispatchStatus(StrEnum):
    PREPARED = "prepared"
    ALREADY_PREPARED = "already-prepared"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StepDispatchError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise StepDispatchError(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise StepDispatchError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise StepDispatchError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise StepDispatchError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise StepDispatchError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise StepDispatchError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise StepDispatchError(f"{name} must be UTC")
    else:
        raise StepDispatchError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StepDispatchError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sequence_tuple(values: object) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise StepDispatchError(
            "appended_event_sequences must be an iterable"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise StepDispatchError(
            "appended_event_sequences must be an iterable"
        ) from exc

    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise StepDispatchError(
                "appended event sequences must be positive integers"
            )
        normalized.append(item)

    if tuple(normalized) != tuple(sorted(set(normalized))):
        raise StepDispatchError(
            "appended event sequences must be unique and increasing"
        )
    return tuple(normalized)


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _plan_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


def _request_source_hash(
    *,
    plan_id: str,
    step_id: str,
    agent_id: str,
    requested_by: str,
    created_at: str,
    resume_application_id: str,
    resume_application_result_hash: str,
    command_id: str,
    command_hash: str,
    plan_state_hash: str,
    journal_event_count: int,
    journal_head_hash: str,
    journal_hash: str,
) -> str:
    return _sha256_document(
        {
            "record_type": "step_dispatch_request_source",
            "plan_id": plan_id,
            "step_id": step_id,
            "agent_id": agent_id,
            "requested_by": requested_by,
            "created_at": created_at,
            "resume_application_id": resume_application_id,
            "resume_application_result_hash": resume_application_result_hash,
            "command_id": command_id,
            "command_hash": command_hash,
            "plan_state_hash": plan_state_hash,
            "journal_event_count": journal_event_count,
            "journal_head_hash": journal_head_hash,
            "journal_hash": journal_hash,
        }
    )


def _marker_payload(
    request: "StepDispatchRequest",
) -> dict[str, Any]:
    return {
        _MARKER_DISPATCH_ID: request.dispatch_id,
        _MARKER_REQUEST_HASH: request.request_hash,
        _MARKER_RESUME_APPLICATION_ID: request.resume_application_id,
        _MARKER_RESUME_RESULT_HASH: (
            request.resume_application_result_hash
        ),
        _MARKER_COMMAND_ID: request.command_id,
        _MARKER_COMMAND_HASH: request.command_hash,
        _MARKER_STEP_ID: request.step_id,
        _MARKER_AGENT_ID: request.agent_id,
    }


@dataclass(frozen=True, slots=True)
class StepDispatchRequest:
    dispatch_id: str
    plan_id: str
    step_id: str
    agent_id: str
    requested_by: str
    created_at: str
    resume_application_id: str
    resume_application_result_hash: str
    command_id: str
    command_hash: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    request_hash: str | None = None
    version: int = STEP_DISPATCH_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dispatch_id",
            _identifier(self.dispatch_id, "dispatch_id"),
        )
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id"),
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
        object.__setattr__(
            self,
            "requested_by",
            _identifier(
                self.requested_by,
                "requested_by",
                _AGENT_ID,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _utc_timestamp(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "resume_application_id",
            _identifier(
                self.resume_application_id,
                "resume_application_id",
            ),
        )
        object.__setattr__(
            self,
            "resume_application_result_hash",
            _hash(
                self.resume_application_result_hash,
                "resume_application_result_hash",
            ),
        )
        object.__setattr__(
            self,
            "command_id",
            _identifier(self.command_id, "command_id"),
        )
        object.__setattr__(
            self,
            "command_hash",
            _hash(self.command_hash, "command_hash"),
        )
        object.__setattr__(
            self,
            "plan_state_hash",
            _hash(self.plan_state_hash, "plan_state_hash"),
        )
        object.__setattr__(
            self,
            "journal_event_count",
            _non_negative_int(
                self.journal_event_count,
                "journal_event_count",
            ),
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

        if self.version != STEP_DISPATCH_FORMAT_VERSION:
            raise StepDispatchError(
                "unsupported step dispatch format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise StepDispatchIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_resume_application(
        cls,
        result: ResumeApplicationResult,
        *,
        step_id: str,
        agent_id: str,
        requested_by: str,
        created_at: str | datetime,
        dispatch_id: str | None = None,
    ) -> "StepDispatchRequest":
        if not isinstance(result, ResumeApplicationResult):
            raise StepDispatchError(
                "result must be a ResumeApplicationResult"
            )
        result.verify_hash()
        journal = result.to_journal()
        seal = journal.seal()
        normalized_time = _utc_timestamp(created_at, "created_at")
        normalized_step = _identifier(step_id, "step_id", _STEP_ID)
        normalized_agent = _identifier(agent_id, "agent_id", _AGENT_ID)
        normalized_requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        result_hash = result.result_hash
        assert result_hash is not None

        source_hash = _request_source_hash(
            plan_id=result.plan_id,
            step_id=normalized_step,
            agent_id=normalized_agent,
            requested_by=normalized_requester,
            created_at=normalized_time,
            resume_application_id=result.application_id,
            resume_application_result_hash=result_hash,
            command_id=result.command_id,
            command_hash=result.command_hash,
            plan_state_hash=_plan_hash(result.updated_plan),
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
        )

        effective_dispatch_id = (
            dispatch_id
            if dispatch_id is not None
            else f"dispatch:{source_hash}"
        )

        return cls(
            dispatch_id=effective_dispatch_id,
            plan_id=result.plan_id,
            step_id=normalized_step,
            agent_id=normalized_agent,
            requested_by=normalized_requester,
            created_at=normalized_time,
            resume_application_id=result.application_id,
            resume_application_result_hash=result_hash,
            command_id=result.command_id,
            command_hash=result.command_hash,
            plan_state_hash=_plan_hash(result.updated_plan),
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "step_dispatch_request",
            "version": self.version,
            "dispatch_id": self.dispatch_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "resume_application_id": self.resume_application_id,
            "resume_application_result_hash": (
                self.resume_application_result_hash
            ),
            "command_id": self.command_id,
            "command_hash": self.command_hash,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise StepDispatchIntegrityError(
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
    ) -> "StepDispatchRequest":
        if data.get("record_type") != "step_dispatch_request":
            raise StepDispatchError(
                "record_type must be step_dispatch_request"
            )
        if "request_hash" not in data:
            raise StepDispatchIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            dispatch_id=data["dispatch_id"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            requested_by=data["requested_by"],
            created_at=data["created_at"],
            resume_application_id=data["resume_application_id"],
            resume_application_result_hash=(
                data["resume_application_result_hash"]
            ),
            command_id=data["command_id"],
            command_hash=data["command_hash"],
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "StepDispatchRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StepDispatchError(
                "step dispatch request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise StepDispatchError(
                "step dispatch request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class StepDispatchResult:
    dispatch_id: str
    status: StepDispatchStatus
    request_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    resume_application_id: str
    resume_application_result_hash: str
    command_id: str
    command_hash: str
    agent_request: AgentRequest
    appended_event_sequences: tuple[int, ...]
    plan_before_hash: str
    plan_after_hash: str
    journal_before_event_count: int
    journal_after_event_count: int
    journal_before_head_hash: str
    journal_after_head_hash: str
    journal_before_hash: str
    journal_after_hash: str
    prepared_at: str
    updated_plan: ExecutionPlan
    updated_events: tuple[ExecutionEvent, ...] = field(repr=False)
    result_hash: str | None = None
    version: int = STEP_DISPATCH_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dispatch_id",
            _identifier(self.dispatch_id, "dispatch_id"),
        )
        try:
            status = StepDispatchStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise StepDispatchError(
                "step dispatch status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        object.__setattr__(
            self,
            "request_hash",
            _hash(self.request_hash, "request_hash"),
        )
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id"),
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
        object.__setattr__(
            self,
            "resume_application_id",
            _identifier(
                self.resume_application_id,
                "resume_application_id",
            ),
        )
        object.__setattr__(
            self,
            "resume_application_result_hash",
            _hash(
                self.resume_application_result_hash,
                "resume_application_result_hash",
            ),
        )
        object.__setattr__(
            self,
            "command_id",
            _identifier(self.command_id, "command_id"),
        )
        object.__setattr__(
            self,
            "command_hash",
            _hash(self.command_hash, "command_hash"),
        )

        if not isinstance(self.agent_request, AgentRequest):
            raise StepDispatchError(
                "agent_request must be an AgentRequest"
            )
        if not isinstance(self.updated_plan, ExecutionPlan):
            raise StepDispatchError(
                "updated_plan must be an ExecutionPlan"
            )
        if self.agent_request.project_id != self.updated_plan.project_id:
            raise StepDispatchError(
                "agent request project does not match updated plan"
            )

        object.__setattr__(
            self,
            "appended_event_sequences",
            _sequence_tuple(self.appended_event_sequences),
        )

        for field_name in (
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
            "journal_before_event_count",
            _non_negative_int(
                self.journal_before_event_count,
                "journal_before_event_count",
            ),
        )
        object.__setattr__(
            self,
            "journal_after_event_count",
            _non_negative_int(
                self.journal_after_event_count,
                "journal_after_event_count",
            ),
        )
        object.__setattr__(
            self,
            "prepared_at",
            _utc_timestamp(self.prepared_at, "prepared_at"),
        )

        if self.updated_plan.plan_id != self.plan_id:
            raise StepDispatchError(
                "updated plan identifier does not match result"
            )

        events = tuple(self.updated_events)
        if not all(isinstance(event, ExecutionEvent) for event in events):
            raise StepDispatchError(
                "updated_events must contain ExecutionEvent values"
            )
        object.__setattr__(self, "updated_events", events)

        reconstructed = ExecutionJournal.from_events(
            self.plan_id,
            events,
        )
        seal = reconstructed.seal()

        if _plan_hash(self.updated_plan) != self.plan_after_hash:
            raise StepDispatchIntegrityError(
                "updated plan does not match plan_after_hash"
            )
        if reconstructed.event_count != self.journal_after_event_count:
            raise StepDispatchIntegrityError(
                "updated journal count does not match result"
            )
        if reconstructed.head_hash != self.journal_after_head_hash:
            raise StepDispatchIntegrityError(
                "updated journal head does not match result"
            )
        if seal.journal_hash != self.journal_after_hash:
            raise StepDispatchIntegrityError(
                "updated journal hash does not match result"
            )

        step = self.updated_plan.get_step(self.step_id)
        if step.status is not StepStatus.RUNNING:
            raise StepDispatchIntegrityError(
                "updated step is not running"
            )
        if step.assigned_agent_id != self.agent_id:
            raise StepDispatchIntegrityError(
                "updated step agent does not match result"
            )
        if self.updated_plan.status is not PlanStatus.RUNNING:
            raise StepDispatchIntegrityError(
                "updated plan is not running"
            )

        expected_agent_request_id = f"agent-request:{self.request_hash}"
        if self.agent_request.request_id != expected_agent_request_id:
            raise StepDispatchIntegrityError(
                "agent request identifier does not match request hash"
            )

        if status is StepDispatchStatus.PREPARED:
            if not self.appended_event_sequences:
                raise StepDispatchError(
                    "prepared result must list appended event sequences"
                )
            expected = tuple(
                range(
                    self.journal_before_event_count + 1,
                    self.journal_after_event_count + 1,
                )
            )
            if self.appended_event_sequences != expected:
                raise StepDispatchError(
                    "appended event sequences are not contiguous"
                )
            if (
                self.journal_after_event_count
                <= self.journal_before_event_count
            ):
                raise StepDispatchError(
                    "prepared result must advance the journal"
                )
        else:
            if self.appended_event_sequences:
                raise StepDispatchError(
                    "already-prepared result cannot append events"
                )
            if (
                self.plan_before_hash != self.plan_after_hash
                or self.journal_before_event_count
                != self.journal_after_event_count
                or self.journal_before_head_hash
                != self.journal_after_head_hash
                or self.journal_before_hash
                != self.journal_after_hash
            ):
                raise StepDispatchError(
                    "already-prepared result cannot change state"
                )

        if self.version != STEP_DISPATCH_FORMAT_VERSION:
            raise StepDispatchError(
                "unsupported step dispatch format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise StepDispatchIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "step_dispatch_result",
            "version": self.version,
            "dispatch_id": self.dispatch_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "resume_application_id": self.resume_application_id,
            "resume_application_result_hash": (
                self.resume_application_result_hash
            ),
            "command_id": self.command_id,
            "command_hash": self.command_hash,
            "agent_request": self.agent_request.to_dict(),
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
            "prepared_at": self.prepared_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise StepDispatchIntegrityError(
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
    ) -> "StepDispatchResult":
        if data.get("record_type") != "step_dispatch_result":
            raise StepDispatchError(
                "record_type must be step_dispatch_result"
            )
        if "result_hash" not in data:
            raise StepDispatchIntegrityError(
                "serialized result is missing result_hash"
            )
        raw_agent_request = data.get("agent_request")
        raw_plan = data.get("updated_plan")
        raw_events = data.get("updated_events")
        if not isinstance(raw_agent_request, Mapping):
            raise StepDispatchError(
                "agent_request must be an object"
            )
        if not isinstance(raw_plan, Mapping):
            raise StepDispatchError(
                "updated_plan must be an object"
            )
        if not isinstance(raw_events, list):
            raise StepDispatchError(
                "updated_events must be a list"
            )

        return cls(
            dispatch_id=data["dispatch_id"],
            status=StepDispatchStatus(data["status"]),
            request_hash=data["request_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            resume_application_id=data["resume_application_id"],
            resume_application_result_hash=(
                data["resume_application_result_hash"]
            ),
            command_id=data["command_id"],
            command_hash=data["command_hash"],
            agent_request=AgentRequest.from_dict(raw_agent_request),
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
            prepared_at=data["prepared_at"],
            updated_plan=ExecutionPlan.from_dict(raw_plan),
            updated_events=tuple(
                ExecutionEvent.from_dict(event)
                for event in raw_events
            ),
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "StepDispatchResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StepDispatchError(
                "step dispatch result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise StepDispatchError(
                "step dispatch result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class StepDispatch:
    request: StepDispatchRequest
    resume_result: ResumeApplicationResult
    registry: AgentRegistry

    def __post_init__(self) -> None:
        if not isinstance(self.request, StepDispatchRequest):
            raise StepDispatchError(
                "request must be a StepDispatchRequest"
            )
        if not isinstance(self.resume_result, ResumeApplicationResult):
            raise StepDispatchError(
                "resume_result must be a ResumeApplicationResult"
            )
        if not isinstance(self.registry, AgentRegistry):
            raise StepDispatchError(
                "registry must be an AgentRegistry"
            )

        self.request.verify_hash()
        self.resume_result.verify_hash()

        result_hash = self.resume_result.result_hash
        assert result_hash is not None

        expected = {
            "plan_id": self.resume_result.plan_id,
            "resume_application_id": self.resume_result.application_id,
            "resume_application_result_hash": result_hash,
            "command_id": self.resume_result.command_id,
            "command_hash": self.resume_result.command_hash,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise StepDispatchError(
                    f"request {field_name} does not match resume result"
                )

        if self.request.step_id not in self.resume_result.selected_step_ids:
            raise StepDispatchError(
                "dispatch step was not authorized by resume application"
            )

    def prepare(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> StepDispatchResult:
        if not isinstance(plan, ExecutionPlan):
            raise StepDispatchError(
                "plan must be an ExecutionPlan"
            )
        if not isinstance(journal, ExecutionJournal):
            raise StepDispatchError(
                "journal must be an ExecutionJournal"
            )

        self.request.verify_hash()
        self.resume_result.verify_hash()
        journal.validate()

        if plan.plan_id != self.request.plan_id:
            raise StepDispatchError(
                "plan identifier does not match dispatch request"
            )
        if journal.plan_id != self.request.plan_id:
            raise StepDispatchError(
                "journal identifier does not match dispatch request"
            )

        existing = self._existing_dispatch_events(journal)
        if existing:
            return self._already_prepared_result(
                plan,
                journal,
                existing,
            )

        self._validate_base_state(plan, journal)
        step = plan.get_step(self.request.step_id)

        if plan.status not in {
            PlanStatus.APPROVED,
            PlanStatus.RUNNING,
        }:
            raise StepDispatchError(
                "plan status cannot prepare a step dispatch"
            )
        if step.status is not StepStatus.APPROVED:
            raise StepDispatchError(
                "dispatch step must be approved"
            )

        incomplete = tuple(
            dependency
            for dependency in step.dependencies
            if plan.get_step(dependency).status
            is not StepStatus.COMPLETED
        )
        if incomplete:
            raise StepDispatchError(
                "dispatch step has incomplete dependencies: "
                + ", ".join(sorted(incomplete))
            )

        if (
            step.assigned_agent_id is not None
            and step.assigned_agent_id != self.request.agent_id
        ):
            raise StepDispatchConflictError(
                "step is assigned to another agent"
            )

        try:
            definition = self.registry.get(self.request.agent_id)
        except UnknownAgentError as exc:
            raise StepDispatchError(
                "dispatch agent is not registered"
            ) from exc

        if not definition.fail_closed:
            raise StepDispatchError(
                "dispatch agent must use fail-closed behavior"
            )
        if not definition.supports(step.capability_id):
            raise StepDispatchError(
                "dispatch agent does not support step capability"
            )

        required = set(step.required_permissions)
        if not required.issubset(definition.permissions):
            raise StepDispatchError(
                "dispatch agent lacks required permissions"
            )

        capability = definition.capability(step.capability_id)
        approval_reference = (
            step.approval_reference
            or plan.approval_reference
        )
        if (
            step.requires_human_approval
            or capability.requires_human_approval
        ) and approval_reference is None:
            raise StepDispatchError(
                "dispatch requires a human approval reference"
            )

        agent_request = self._build_agent_request(
            plan,
            step,
            approval_reference,
        )

        before_plan_hash = _plan_hash(plan)
        before_seal = journal.seal()
        updated_plan = self._updated_plan(plan)
        updated_journal = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events,
        )

        marker = _marker_payload(self.request)
        appended: list[int] = []

        if plan.status is PlanStatus.APPROVED:
            event = updated_journal.append(
                ExecutionEventType.PLAN_STARTED,
                self.request.created_at,
                agent_id=self.request.requested_by,
                payload=marker,
            )
            appended.append(event.sequence)

        event = updated_journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            self.request.created_at,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            payload=marker,
        )
        appended.append(event.sequence)

        event = updated_journal.append(
            ExecutionEventType.STEP_STARTED,
            self.request.created_at,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            payload=marker,
        )
        appended.append(event.sequence)

        ExecutionCheckpoint.capture(
            updated_plan,
            updated_journal,
            checkpoint_id=f"dispatch-validation:{self.request.request_hash}",
            created_at=self.request.created_at,
        )

        after_seal = updated_journal.seal()
        result_hash = self.resume_result.result_hash
        assert result_hash is not None
        request_hash = self.request.request_hash
        assert request_hash is not None

        return StepDispatchResult(
            dispatch_id=self.request.dispatch_id,
            status=StepDispatchStatus.PREPARED,
            request_hash=request_hash,
            plan_id=self.request.plan_id,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            resume_application_id=self.request.resume_application_id,
            resume_application_result_hash=result_hash,
            command_id=self.request.command_id,
            command_hash=self.request.command_hash,
            agent_request=agent_request,
            appended_event_sequences=tuple(appended),
            plan_before_hash=before_plan_hash,
            plan_after_hash=_plan_hash(updated_plan),
            journal_before_event_count=before_seal.event_count,
            journal_after_event_count=after_seal.event_count,
            journal_before_head_hash=before_seal.head_hash,
            journal_after_head_hash=after_seal.head_hash,
            journal_before_hash=before_seal.journal_hash,
            journal_after_hash=after_seal.journal_hash,
            prepared_at=self.request.created_at,
            updated_plan=updated_plan,
            updated_events=updated_journal.events,
        )

    def _validate_base_state(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> None:
        current_seal = journal.seal()
        if _plan_hash(plan) != self.request.plan_state_hash:
            raise StepDispatchError(
                "plan state differs from dispatch request boundary"
            )
        if current_seal.event_count != self.request.journal_event_count:
            raise StepDispatchError(
                "journal event count differs from dispatch request boundary"
            )
        if current_seal.head_hash != self.request.journal_head_hash:
            raise StepDispatchError(
                "journal head differs from dispatch request boundary"
            )
        if current_seal.journal_hash != self.request.journal_hash:
            raise StepDispatchError(
                "journal hash differs from dispatch request boundary"
            )

        result_journal = self.resume_result.to_journal()
        if plan.to_json() != self.resume_result.updated_plan.to_json():
            raise StepDispatchError(
                "plan does not match resume application result"
            )
        if journal.to_jsonl() != result_journal.to_jsonl():
            raise StepDispatchError(
                "journal does not match resume application result"
            )

    def _build_agent_request(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
        approval_reference: str | None,
    ) -> AgentRequest:
        request_hash = self.request.request_hash
        assert request_hash is not None

        return AgentRequest(
            request_id=f"agent-request:{request_hash}",
            project_id=plan.project_id,
            capability_id=step.capability_id,
            objective=step.objective,
            requested_by=self.request.requested_by,
            inputs={
                "plan_id": plan.plan_id,
                "step_id": step.step_id,
                "step_title": step.title,
                "dependencies": list(step.dependencies),
                "resume_application_id": (
                    self.request.resume_application_id
                ),
                "resume_application_result_hash": (
                    self.request.resume_application_result_hash
                ),
                "resume_command_id": self.request.command_id,
                "resume_command_hash": self.request.command_hash,
                "step_metadata": dict(step.metadata),
            },
            constraints={
                "assigned_agent_id": self.request.agent_id,
                "required_permissions": list(
                    step.required_permissions
                ),
                "execution_mode": "dispatch-preparation-only",
                "provider_call_allowed": False,
                "generated_code_execution_allowed": False,
                "project_write_allowed": False,
            },
            approval_reference=approval_reference,
        )

    def _updated_plan(
        self,
        plan: ExecutionPlan,
    ) -> ExecutionPlan:
        updated_steps = []
        for step in plan.steps:
            if step.step_id != self.request.step_id:
                updated_steps.append(step)
                continue
            updated_steps.append(
                replace(
                    step,
                    assigned_agent_id=self.request.agent_id,
                    status=StepStatus.RUNNING,
                )
            )

        return replace(
            plan,
            steps=tuple(updated_steps),
            status=PlanStatus.RUNNING,
        )

    def _existing_dispatch_events(
        self,
        journal: ExecutionJournal,
    ) -> tuple[ExecutionEvent, ...]:
        request_hash = self.request.request_hash
        assert request_hash is not None

        for event in journal.events:
            event_dispatch_id = event.payload.get(
                _MARKER_DISPATCH_ID
            )
            event_request_hash = event.payload.get(
                _MARKER_REQUEST_HASH
            )
            if (
                event_dispatch_id == self.request.dispatch_id
                and event_request_hash is not None
                and event_request_hash != request_hash
            ):
                raise StepDispatchConflictError(
                    "dispatch identifier already exists with another request hash"
                )

        matches = tuple(
            event
            for event in journal.events
            if event.payload.get(_MARKER_REQUEST_HASH)
            == request_hash
        )
        if not matches:
            return ()

        expected_types = (
            (
                ExecutionEventType.PLAN_STARTED,
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
            )
            if self.resume_result.updated_plan.status
            is PlanStatus.APPROVED
            else (
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
            )
        )

        if len(matches) != len(expected_types):
            raise StepDispatchIntegrityError(
                "dispatch marker count is incomplete or duplicated"
            )

        expected_sequences = tuple(
            range(
                self.request.journal_event_count + 1,
                self.request.journal_event_count
                + len(expected_types)
                + 1,
            )
        )
        actual_sequences = tuple(event.sequence for event in matches)
        if actual_sequences != expected_sequences:
            raise StepDispatchIntegrityError(
                "dispatch markers are not contiguous at request boundary"
            )

        expected_payload = _marker_payload(self.request)
        for event, expected_type in zip(
            matches,
            expected_types,
            strict=True,
        ):
            if event.event_type is not expected_type:
                raise StepDispatchIntegrityError(
                    "dispatch marker uses an unexpected event type"
                )
            for key, expected_value in expected_payload.items():
                if event.payload.get(key) != expected_value:
                    raise StepDispatchIntegrityError(
                        f"dispatch marker field {key} does not match request"
                    )

            if expected_type is ExecutionEventType.PLAN_STARTED:
                if event.step_id is not None:
                    raise StepDispatchIntegrityError(
                        "plan dispatch marker cannot reference a step"
                    )
                if event.agent_id != self.request.requested_by:
                    raise StepDispatchIntegrityError(
                        "plan dispatch marker requester does not match"
                    )
            else:
                if event.step_id != self.request.step_id:
                    raise StepDispatchIntegrityError(
                        "step dispatch marker step does not match"
                    )
                if event.agent_id != self.request.agent_id:
                    raise StepDispatchIntegrityError(
                        "step dispatch marker agent does not match"
                    )

        return matches

    def _already_prepared_result(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        existing: tuple[ExecutionEvent, ...],
    ) -> StepDispatchResult:
        ExecutionCheckpoint.capture(
            plan,
            journal,
            checkpoint_id=f"dispatch-replay:{self.request.request_hash}",
            created_at=existing[0].timestamp,
        )

        if plan.status is not PlanStatus.RUNNING:
            raise StepDispatchIntegrityError(
                "replayed dispatch requires a running plan"
            )
        step = plan.get_step(self.request.step_id)
        if step.status is not StepStatus.RUNNING:
            raise StepDispatchIntegrityError(
                "replayed dispatch requires a running step"
            )
        if step.assigned_agent_id != self.request.agent_id:
            raise StepDispatchIntegrityError(
                "replayed dispatch agent assignment differs"
            )

        approval_reference = (
            step.approval_reference
            or plan.approval_reference
        )
        agent_request = self._build_agent_request(
            plan,
            step,
            approval_reference,
        )

        current_plan_hash = _plan_hash(plan)
        current_seal = journal.seal()
        result_hash = self.resume_result.result_hash
        assert result_hash is not None
        request_hash = self.request.request_hash
        assert request_hash is not None

        return StepDispatchResult(
            dispatch_id=self.request.dispatch_id,
            status=StepDispatchStatus.ALREADY_PREPARED,
            request_hash=request_hash,
            plan_id=self.request.plan_id,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            resume_application_id=self.request.resume_application_id,
            resume_application_result_hash=result_hash,
            command_id=self.request.command_id,
            command_hash=self.request.command_hash,
            agent_request=agent_request,
            appended_event_sequences=(),
            plan_before_hash=current_plan_hash,
            plan_after_hash=current_plan_hash,
            journal_before_event_count=current_seal.event_count,
            journal_after_event_count=current_seal.event_count,
            journal_before_head_hash=current_seal.head_hash,
            journal_after_head_hash=current_seal.head_hash,
            journal_before_hash=current_seal.journal_hash,
            journal_after_hash=current_seal.journal_hash,
            prepared_at=existing[0].timestamp,
            updated_plan=plan,
            updated_events=journal.events,
        )
