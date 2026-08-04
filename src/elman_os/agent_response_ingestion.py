"""Controlled ingestion of one agent response for ELMAN-OS v0.7."""

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
    AgentResponse,
    AgentResponseStatus,
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
from .step_dispatch import StepDispatchResult


AGENT_RESPONSE_INGESTION_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_MARKER_INGESTION_ID = "agent_response_ingestion_id"
_MARKER_INGESTION_REQUEST_HASH = "agent_response_ingestion_request_hash"
_MARKER_RESPONSE_HASH = "agent_response_hash"
_MARKER_RESPONSE_REQUEST_ID = "agent_response_request_id"
_MARKER_RESPONSE_STATUS = "agent_response_status"
_MARKER_DISPATCH_ID = "step_dispatch_id"
_MARKER_DISPATCH_RESULT_HASH = "step_dispatch_result_hash"
_MARKER_STEP_ID = "agent_response_step_id"
_MARKER_AGENT_ID = "agent_response_agent_id"


class AgentResponseIngestionError(ValueError):
    """An agent response cannot be ingested safely."""


class AgentResponseIngestionIntegrityError(AgentResponseIngestionError):
    """An ingestion request, marker, response, or result is inconsistent."""


class AgentResponseIngestionConflictError(AgentResponseIngestionError):
    """An ingestion identifier or response conflicts with journal history."""


class AgentResponseIngestionStatus(StrEnum):
    INGESTED = "ingested"
    ALREADY_INGESTED = "already-ingested"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentResponseIngestionError(
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
        raise AgentResponseIngestionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise AgentResponseIngestionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise AgentResponseIngestionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise AgentResponseIngestionError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise AgentResponseIngestionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise AgentResponseIngestionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise AgentResponseIngestionError(
                f"{name} must be UTC"
            )
    else:
        raise AgentResponseIngestionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _non_negative_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise AgentResponseIngestionError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sequence_tuple(values: object) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentResponseIngestionError(
            "appended_event_sequences must be an iterable"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise AgentResponseIngestionError(
            "appended_event_sequences must be an iterable"
        ) from exc

    normalized: list[int] = []
    for item in items:
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 1
        ):
            raise AgentResponseIngestionError(
                "appended event sequences must be positive integers"
            )
        normalized.append(item)

    if tuple(normalized) != tuple(sorted(set(normalized))):
        raise AgentResponseIngestionError(
            "appended event sequences must be unique and increasing"
        )
    return tuple(normalized)


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _plan_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(
        plan.to_json().encode("utf-8")
    ).hexdigest()


def _response_hash(response: AgentResponse) -> str:
    return hashlib.sha256(
        response.to_json().encode("utf-8")
    ).hexdigest()


def _target_step_status(
    status: AgentResponseStatus,
) -> StepStatus:
    return {
        AgentResponseStatus.SUCCEEDED: StepStatus.COMPLETED,
        AgentResponseStatus.BLOCKED: StepStatus.BLOCKED,
        AgentResponseStatus.FAILED: StepStatus.FAILED,
    }[status]


def _step_event_type(
    status: AgentResponseStatus,
) -> ExecutionEventType:
    return {
        AgentResponseStatus.SUCCEEDED: (
            ExecutionEventType.STEP_COMPLETED
        ),
        AgentResponseStatus.BLOCKED: (
            ExecutionEventType.STEP_BLOCKED
        ),
        AgentResponseStatus.FAILED: (
            ExecutionEventType.STEP_FAILED
        ),
    }[status]


def _plan_terminal_event_type(
    status: AgentResponseStatus,
    *,
    all_steps_completed: bool,
) -> ExecutionEventType | None:
    if status is AgentResponseStatus.BLOCKED:
        return ExecutionEventType.PLAN_BLOCKED
    if status is AgentResponseStatus.FAILED:
        return ExecutionEventType.PLAN_FAILED
    if all_steps_completed:
        return ExecutionEventType.PLAN_COMPLETED
    return None


def _target_plan_status(
    status: AgentResponseStatus,
    *,
    all_steps_completed: bool,
) -> PlanStatus:
    if status is AgentResponseStatus.BLOCKED:
        return PlanStatus.BLOCKED
    if status is AgentResponseStatus.FAILED:
        return PlanStatus.FAILED
    if all_steps_completed:
        return PlanStatus.COMPLETED
    return PlanStatus.RUNNING


def _marker_payload(
    request: "AgentResponseIngestionRequest",
) -> dict[str, Any]:
    request_hash = request.request_hash
    assert request_hash is not None
    return {
        _MARKER_INGESTION_ID: request.ingestion_id,
        _MARKER_INGESTION_REQUEST_HASH: request_hash,
        _MARKER_RESPONSE_HASH: request.response_hash,
        _MARKER_RESPONSE_REQUEST_ID: request.agent_request_id,
        _MARKER_RESPONSE_STATUS: request.response_status.value,
        _MARKER_DISPATCH_ID: request.dispatch_id,
        _MARKER_DISPATCH_RESULT_HASH: request.dispatch_result_hash,
        _MARKER_STEP_ID: request.step_id,
        _MARKER_AGENT_ID: request.agent_id,
    }


@dataclass(frozen=True, slots=True)
class AgentResponseIngestionRequest:
    ingestion_id: str
    dispatch_id: str
    dispatch_result_hash: str
    plan_id: str
    step_id: str
    agent_request_id: str
    agent_id: str
    response_status: AgentResponseStatus
    response_hash: str
    received_at: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    request_hash: str | None = None
    version: int = AGENT_RESPONSE_INGESTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ingestion_id",
            _identifier(self.ingestion_id, "ingestion_id"),
        )
        object.__setattr__(
            self,
            "dispatch_id",
            _identifier(self.dispatch_id, "dispatch_id"),
        )
        object.__setattr__(
            self,
            "dispatch_result_hash",
            _hash(
                self.dispatch_result_hash,
                "dispatch_result_hash",
            ),
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
            "agent_request_id",
            _identifier(
                self.agent_request_id,
                "agent_request_id",
            ),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        try:
            response_status = AgentResponseStatus(
                self.response_status
            )
        except (TypeError, ValueError) as exc:
            raise AgentResponseIngestionError(
                "response_status is invalid"
            ) from exc
        object.__setattr__(
            self,
            "response_status",
            response_status,
        )
        object.__setattr__(
            self,
            "response_hash",
            _hash(self.response_hash, "response_hash"),
        )
        object.__setattr__(
            self,
            "received_at",
            _utc_timestamp(self.received_at, "received_at"),
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

        if (
            self.version
            != AGENT_RESPONSE_INGESTION_FORMAT_VERSION
        ):
            raise AgentResponseIngestionError(
                "unsupported agent response ingestion format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise AgentResponseIngestionIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_dispatch_result(
        cls,
        dispatch_result: StepDispatchResult,
        response: AgentResponse,
        *,
        received_at: str | datetime,
        ingestion_id: str | None = None,
    ) -> "AgentResponseIngestionRequest":
        if not isinstance(dispatch_result, StepDispatchResult):
            raise AgentResponseIngestionError(
                "dispatch_result must be a StepDispatchResult"
            )
        if not isinstance(response, AgentResponse):
            raise AgentResponseIngestionError(
                "response must be an AgentResponse"
            )

        dispatch_result.verify_hash()

        if (
            response.request_id
            != dispatch_result.agent_request.request_id
        ):
            raise AgentResponseIngestionError(
                "response request_id does not match dispatch agent request"
            )
        if response.agent_id != dispatch_result.agent_id:
            raise AgentResponseIngestionError(
                "response agent_id does not match dispatch agent"
            )

        result_hash = dispatch_result.result_hash
        assert result_hash is not None
        seal = dispatch_result.to_journal().seal()
        normalized_received_at = _utc_timestamp(
            received_at,
            "received_at",
        )
        response_digest = _response_hash(response)

        source_hash = _sha256_document(
            {
                "record_type": (
                    "agent_response_ingestion_request_source"
                ),
                "dispatch_id": dispatch_result.dispatch_id,
                "dispatch_result_hash": result_hash,
                "plan_id": dispatch_result.plan_id,
                "step_id": dispatch_result.step_id,
                "agent_request_id": (
                    dispatch_result.agent_request.request_id
                ),
                "agent_id": dispatch_result.agent_id,
                "response_status": response.status.value,
                "response_hash": response_digest,
                "received_at": normalized_received_at,
                "plan_state_hash": _plan_hash(
                    dispatch_result.updated_plan
                ),
                "journal_event_count": seal.event_count,
                "journal_head_hash": seal.head_hash,
                "journal_hash": seal.journal_hash,
            }
        )
        effective_id = (
            ingestion_id
            if ingestion_id is not None
            else f"ingestion:{source_hash}"
        )

        return cls(
            ingestion_id=effective_id,
            dispatch_id=dispatch_result.dispatch_id,
            dispatch_result_hash=result_hash,
            plan_id=dispatch_result.plan_id,
            step_id=dispatch_result.step_id,
            agent_request_id=(
                dispatch_result.agent_request.request_id
            ),
            agent_id=dispatch_result.agent_id,
            response_status=response.status,
            response_hash=response_digest,
            received_at=normalized_received_at,
            plan_state_hash=_plan_hash(
                dispatch_result.updated_plan
            ),
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "agent_response_ingestion_request",
            "version": self.version,
            "ingestion_id": self.ingestion_id,
            "dispatch_id": self.dispatch_id,
            "dispatch_result_hash": self.dispatch_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_request_id": self.agent_request_id,
            "agent_id": self.agent_id,
            "response_status": self.response_status.value,
            "response_hash": self.response_hash,
            "received_at": self.received_at,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise AgentResponseIngestionIntegrityError(
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
    ) -> "AgentResponseIngestionRequest":
        if (
            data.get("record_type")
            != "agent_response_ingestion_request"
        ):
            raise AgentResponseIngestionError(
                "record_type must be agent_response_ingestion_request"
            )
        if "request_hash" not in data:
            raise AgentResponseIngestionIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            ingestion_id=data["ingestion_id"],
            dispatch_id=data["dispatch_id"],
            dispatch_result_hash=data["dispatch_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_request_id=data["agent_request_id"],
            agent_id=data["agent_id"],
            response_status=AgentResponseStatus(
                data["response_status"]
            ),
            response_hash=data["response_hash"],
            received_at=data["received_at"],
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "AgentResponseIngestionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentResponseIngestionError(
                "agent response ingestion request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise AgentResponseIngestionError(
                "agent response ingestion request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class AgentResponseIngestionResult:
    ingestion_id: str
    status: AgentResponseIngestionStatus
    request_hash: str
    dispatch_id: str
    dispatch_result_hash: str
    plan_id: str
    step_id: str
    agent_request_id: str
    agent_id: str
    response_hash: str
    response: AgentResponse
    appended_event_sequences: tuple[int, ...]
    plan_before_hash: str
    plan_after_hash: str
    journal_before_event_count: int
    journal_after_event_count: int
    journal_before_head_hash: str
    journal_after_head_hash: str
    journal_before_hash: str
    journal_after_hash: str
    received_at: str
    updated_plan: ExecutionPlan
    updated_events: tuple[ExecutionEvent, ...] = field(repr=False)
    result_hash: str | None = None
    version: int = AGENT_RESPONSE_INGESTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ingestion_id",
            _identifier(self.ingestion_id, "ingestion_id"),
        )
        try:
            status = AgentResponseIngestionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise AgentResponseIngestionError(
                "agent response ingestion status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        object.__setattr__(
            self,
            "request_hash",
            _hash(self.request_hash, "request_hash"),
        )
        object.__setattr__(
            self,
            "dispatch_id",
            _identifier(self.dispatch_id, "dispatch_id"),
        )
        object.__setattr__(
            self,
            "dispatch_result_hash",
            _hash(
                self.dispatch_result_hash,
                "dispatch_result_hash",
            ),
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
            "agent_request_id",
            _identifier(
                self.agent_request_id,
                "agent_request_id",
            ),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "response_hash",
            _hash(self.response_hash, "response_hash"),
        )

        if not isinstance(self.response, AgentResponse):
            raise AgentResponseIngestionError(
                "response must be an AgentResponse"
            )
        if self.response.request_id != self.agent_request_id:
            raise AgentResponseIngestionIntegrityError(
                "response request_id does not match result"
            )
        if self.response.agent_id != self.agent_id:
            raise AgentResponseIngestionIntegrityError(
                "response agent_id does not match result"
            )
        if _response_hash(self.response) != self.response_hash:
            raise AgentResponseIngestionIntegrityError(
                "response content does not match response_hash"
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
            "received_at",
            _utc_timestamp(self.received_at, "received_at"),
        )

        if not isinstance(self.updated_plan, ExecutionPlan):
            raise AgentResponseIngestionError(
                "updated_plan must be an ExecutionPlan"
            )
        if self.updated_plan.plan_id != self.plan_id:
            raise AgentResponseIngestionIntegrityError(
                "updated plan identifier does not match result"
            )

        events = tuple(self.updated_events)
        if not all(
            isinstance(event, ExecutionEvent)
            for event in events
        ):
            raise AgentResponseIngestionError(
                "updated_events must contain ExecutionEvent values"
            )
        object.__setattr__(self, "updated_events", events)

        reconstructed = ExecutionJournal.from_events(
            self.plan_id,
            events,
        )
        seal = reconstructed.seal()

        if _plan_hash(self.updated_plan) != self.plan_after_hash:
            raise AgentResponseIngestionIntegrityError(
                "updated plan does not match plan_after_hash"
            )
        if (
            reconstructed.event_count
            != self.journal_after_event_count
        ):
            raise AgentResponseIngestionIntegrityError(
                "updated journal count does not match result"
            )
        if reconstructed.head_hash != self.journal_after_head_hash:
            raise AgentResponseIngestionIntegrityError(
                "updated journal head does not match result"
            )
        if seal.journal_hash != self.journal_after_hash:
            raise AgentResponseIngestionIntegrityError(
                "updated journal hash does not match result"
            )

        step = self.updated_plan.get_step(self.step_id)
        expected_step_status = _target_step_status(
            self.response.status
        )
        if step.status is not expected_step_status:
            raise AgentResponseIngestionIntegrityError(
                "updated step status does not match response status"
            )
        if step.assigned_agent_id != self.agent_id:
            raise AgentResponseIngestionIntegrityError(
                "updated step agent does not match result"
            )

        all_completed = all(
            item.status is StepStatus.COMPLETED
            for item in self.updated_plan.steps
        )
        expected_plan_status = _target_plan_status(
            self.response.status,
            all_steps_completed=all_completed,
        )
        if self.updated_plan.status is not expected_plan_status:
            raise AgentResponseIngestionIntegrityError(
                "updated plan status does not match response outcome"
            )

        if status is AgentResponseIngestionStatus.INGESTED:
            if not self.appended_event_sequences:
                raise AgentResponseIngestionError(
                    "ingested result must list appended event sequences"
                )
            expected_sequences = tuple(
                range(
                    self.journal_before_event_count + 1,
                    self.journal_after_event_count + 1,
                )
            )
            if (
                self.appended_event_sequences
                != expected_sequences
            ):
                raise AgentResponseIngestionError(
                    "appended event sequences are not contiguous"
                )
            if (
                self.journal_after_event_count
                <= self.journal_before_event_count
            ):
                raise AgentResponseIngestionError(
                    "ingested result must advance the journal"
                )
        else:
            if self.appended_event_sequences:
                raise AgentResponseIngestionError(
                    "already-ingested result cannot append events"
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
                raise AgentResponseIngestionError(
                    "already-ingested result cannot change state"
                )

        if (
            self.version
            != AGENT_RESPONSE_INGESTION_FORMAT_VERSION
        ):
            raise AgentResponseIngestionError(
                "unsupported agent response ingestion format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise AgentResponseIngestionIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "agent_response_ingestion_result",
            "version": self.version,
            "ingestion_id": self.ingestion_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "dispatch_id": self.dispatch_id,
            "dispatch_result_hash": self.dispatch_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_request_id": self.agent_request_id,
            "agent_id": self.agent_id,
            "response_hash": self.response_hash,
            "response": self.response.to_dict(),
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
            "received_at": self.received_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise AgentResponseIngestionIntegrityError(
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
    ) -> "AgentResponseIngestionResult":
        if (
            data.get("record_type")
            != "agent_response_ingestion_result"
        ):
            raise AgentResponseIngestionError(
                "record_type must be agent_response_ingestion_result"
            )
        if "result_hash" not in data:
            raise AgentResponseIngestionIntegrityError(
                "serialized result is missing result_hash"
            )
        raw_response = data.get("response")
        raw_plan = data.get("updated_plan")
        raw_events = data.get("updated_events")
        if not isinstance(raw_response, Mapping):
            raise AgentResponseIngestionError(
                "response must be an object"
            )
        if not isinstance(raw_plan, Mapping):
            raise AgentResponseIngestionError(
                "updated_plan must be an object"
            )
        if not isinstance(raw_events, list):
            raise AgentResponseIngestionError(
                "updated_events must be a list"
            )

        return cls(
            ingestion_id=data["ingestion_id"],
            status=AgentResponseIngestionStatus(data["status"]),
            request_hash=data["request_hash"],
            dispatch_id=data["dispatch_id"],
            dispatch_result_hash=data["dispatch_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_request_id=data["agent_request_id"],
            agent_id=data["agent_id"],
            response_hash=data["response_hash"],
            response=AgentResponse.from_dict(raw_response),
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
            received_at=data["received_at"],
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
    ) -> "AgentResponseIngestionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentResponseIngestionError(
                "agent response ingestion result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise AgentResponseIngestionError(
                "agent response ingestion result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class AgentResponseIngestion:
    request: AgentResponseIngestionRequest
    dispatch_result: StepDispatchResult
    response: AgentResponse

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            AgentResponseIngestionRequest,
        ):
            raise AgentResponseIngestionError(
                "request must be an AgentResponseIngestionRequest"
            )
        if not isinstance(
            self.dispatch_result,
            StepDispatchResult,
        ):
            raise AgentResponseIngestionError(
                "dispatch_result must be a StepDispatchResult"
            )
        if not isinstance(self.response, AgentResponse):
            raise AgentResponseIngestionError(
                "response must be an AgentResponse"
            )

        self.request.verify_hash()
        self.dispatch_result.verify_hash()

        result_hash = self.dispatch_result.result_hash
        assert result_hash is not None

        expected = {
            "dispatch_id": self.dispatch_result.dispatch_id,
            "dispatch_result_hash": result_hash,
            "plan_id": self.dispatch_result.plan_id,
            "step_id": self.dispatch_result.step_id,
            "agent_request_id": (
                self.dispatch_result.agent_request.request_id
            ),
            "agent_id": self.dispatch_result.agent_id,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise AgentResponseIngestionError(
                    f"request {field_name} does not match dispatch result"
                )

        if self.response.request_id != self.request.agent_request_id:
            raise AgentResponseIngestionError(
                "response request_id does not match ingestion request"
            )
        if self.response.agent_id != self.request.agent_id:
            raise AgentResponseIngestionError(
                "response agent_id does not match ingestion request"
            )
        if self.response.status is not self.request.response_status:
            raise AgentResponseIngestionError(
                "response status does not match ingestion request"
            )
        if _response_hash(self.response) != self.request.response_hash:
            raise AgentResponseIngestionIntegrityError(
                "response content does not match ingestion request hash"
            )

    def ingest(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
    ) -> AgentResponseIngestionResult:
        if not isinstance(plan, ExecutionPlan):
            raise AgentResponseIngestionError(
                "plan must be an ExecutionPlan"
            )
        if not isinstance(journal, ExecutionJournal):
            raise AgentResponseIngestionError(
                "journal must be an ExecutionJournal"
            )

        self.request.verify_hash()
        self.dispatch_result.verify_hash()
        journal.validate()

        if plan.plan_id != self.request.plan_id:
            raise AgentResponseIngestionError(
                "plan identifier does not match ingestion request"
            )
        if journal.plan_id != self.request.plan_id:
            raise AgentResponseIngestionError(
                "journal identifier does not match ingestion request"
            )

        existing = self._existing_ingestion_events(journal)
        if existing:
            return self._already_ingested_result(
                plan,
                journal,
                existing,
            )

        self._validate_base_state(plan, journal)
        step = plan.get_step(self.request.step_id)

        if plan.status is not PlanStatus.RUNNING:
            raise AgentResponseIngestionError(
                "plan must be running before response ingestion"
            )
        if step.status is not StepStatus.RUNNING:
            raise AgentResponseIngestionError(
                "response step must be running"
            )
        if step.assigned_agent_id != self.request.agent_id:
            raise AgentResponseIngestionConflictError(
                "running step is assigned to another agent"
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

        event = updated_journal.append(
            _step_event_type(self.response.status),
            self.request.received_at,
            step_id=self.request.step_id,
            agent_id=self.request.agent_id,
            payload=marker,
        )
        appended.append(event.sequence)

        all_completed = all(
            item.status is StepStatus.COMPLETED
            for item in updated_plan.steps
        )
        plan_event_type = _plan_terminal_event_type(
            self.response.status,
            all_steps_completed=all_completed,
        )
        if plan_event_type is not None:
            event = updated_journal.append(
                plan_event_type,
                self.request.received_at,
                agent_id=self.request.agent_id,
                payload=marker,
            )
            appended.append(event.sequence)

        ExecutionCheckpoint.capture(
            updated_plan,
            updated_journal,
            checkpoint_id=(
                f"response-ingestion-validation:"
                f"{self.request.request_hash}"
            ),
            created_at=self.request.received_at,
        )

        after_seal = updated_journal.seal()
        request_hash = self.request.request_hash
        assert request_hash is not None
        dispatch_result_hash = self.dispatch_result.result_hash
        assert dispatch_result_hash is not None

        return AgentResponseIngestionResult(
            ingestion_id=self.request.ingestion_id,
            status=AgentResponseIngestionStatus.INGESTED,
            request_hash=request_hash,
            dispatch_id=self.request.dispatch_id,
            dispatch_result_hash=dispatch_result_hash,
            plan_id=self.request.plan_id,
            step_id=self.request.step_id,
            agent_request_id=self.request.agent_request_id,
            agent_id=self.request.agent_id,
            response_hash=self.request.response_hash,
            response=self.response,
            appended_event_sequences=tuple(appended),
            plan_before_hash=before_plan_hash,
            plan_after_hash=_plan_hash(updated_plan),
            journal_before_event_count=before_seal.event_count,
            journal_after_event_count=after_seal.event_count,
            journal_before_head_hash=before_seal.head_hash,
            journal_after_head_hash=after_seal.head_hash,
            journal_before_hash=before_seal.journal_hash,
            journal_after_hash=after_seal.journal_hash,
            received_at=self.request.received_at,
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
            raise AgentResponseIngestionError(
                "plan state differs from ingestion request boundary"
            )
        if (
            current_seal.event_count
            != self.request.journal_event_count
        ):
            raise AgentResponseIngestionError(
                "journal event count differs from ingestion request boundary"
            )
        if current_seal.head_hash != self.request.journal_head_hash:
            raise AgentResponseIngestionError(
                "journal head differs from ingestion request boundary"
            )
        if current_seal.journal_hash != self.request.journal_hash:
            raise AgentResponseIngestionError(
                "journal hash differs from ingestion request boundary"
            )

        dispatch_journal = self.dispatch_result.to_journal()
        if (
            plan.to_json()
            != self.dispatch_result.updated_plan.to_json()
        ):
            raise AgentResponseIngestionError(
                "plan does not match step dispatch result"
            )
        if journal.to_jsonl() != dispatch_journal.to_jsonl():
            raise AgentResponseIngestionError(
                "journal does not match step dispatch result"
            )

    def _updated_plan(
        self,
        plan: ExecutionPlan,
    ) -> ExecutionPlan:
        target_step_status = _target_step_status(
            self.response.status
        )
        updated_steps: list[ExecutionStep] = []

        for step in plan.steps:
            if step.step_id != self.request.step_id:
                updated_steps.append(step)
                continue
            updated_steps.append(
                replace(
                    step,
                    status=target_step_status,
                )
            )

        all_completed = all(
            step.status is StepStatus.COMPLETED
            for step in updated_steps
        )
        target_plan_status = _target_plan_status(
            self.response.status,
            all_steps_completed=all_completed,
        )

        return replace(
            plan,
            steps=tuple(updated_steps),
            status=target_plan_status,
        )

    def _expected_event_types(
        self,
    ) -> tuple[ExecutionEventType, ...]:
        updated_plan = self._updated_plan(
            self.dispatch_result.updated_plan
        )
        all_completed = all(
            item.status is StepStatus.COMPLETED
            for item in updated_plan.steps
        )
        types: list[ExecutionEventType] = [
            _step_event_type(self.response.status)
        ]
        plan_type = _plan_terminal_event_type(
            self.response.status,
            all_steps_completed=all_completed,
        )
        if plan_type is not None:
            types.append(plan_type)
        return tuple(types)

    def _existing_ingestion_events(
        self,
        journal: ExecutionJournal,
    ) -> tuple[ExecutionEvent, ...]:
        request_hash = self.request.request_hash
        assert request_hash is not None

        for event in journal.events:
            payload = event.payload
            historical_ingestion_id = payload.get(
                _MARKER_INGESTION_ID
            )
            historical_request_hash = payload.get(
                _MARKER_INGESTION_REQUEST_HASH
            )
            if (
                historical_ingestion_id
                == self.request.ingestion_id
                and historical_request_hash is not None
                and historical_request_hash != request_hash
            ):
                raise AgentResponseIngestionConflictError(
                    "ingestion identifier already exists with another request hash"
                )

            historical_response_request_id = payload.get(
                _MARKER_RESPONSE_REQUEST_ID
            )
            historical_response_hash = payload.get(
                _MARKER_RESPONSE_HASH
            )
            if (
                historical_response_request_id
                == self.request.agent_request_id
                and historical_response_hash is not None
                and historical_response_hash
                != self.request.response_hash
            ):
                raise AgentResponseIngestionConflictError(
                    "agent request already has a different response hash"
                )

        matches = tuple(
            event
            for event in journal.events
            if event.payload.get(
                _MARKER_INGESTION_REQUEST_HASH
            )
            == request_hash
        )
        if not matches:
            return ()

        expected_types = self._expected_event_types()
        if len(matches) != len(expected_types):
            raise AgentResponseIngestionIntegrityError(
                "ingestion marker count is incomplete or duplicated"
            )

        expected_sequences = tuple(
            range(
                self.request.journal_event_count + 1,
                self.request.journal_event_count
                + len(expected_types)
                + 1,
            )
        )
        actual_sequences = tuple(
            event.sequence
            for event in matches
        )
        if actual_sequences != expected_sequences:
            raise AgentResponseIngestionIntegrityError(
                "ingestion markers are not contiguous at request boundary"
            )

        if journal.event_count != expected_sequences[-1]:
            raise AgentResponseIngestionIntegrityError(
                "journal advanced beyond the exact ingestion replay boundary"
            )

        expected_payload = _marker_payload(self.request)
        for event, expected_type in zip(
            matches,
            expected_types,
            strict=True,
        ):
            if event.event_type is not expected_type:
                raise AgentResponseIngestionIntegrityError(
                    "ingestion marker uses an unexpected event type"
                )
            for key, expected_value in expected_payload.items():
                if event.payload.get(key) != expected_value:
                    raise AgentResponseIngestionIntegrityError(
                        f"ingestion marker field {key} does not match request"
                    )

            if expected_type in {
                ExecutionEventType.STEP_COMPLETED,
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.STEP_FAILED,
            }:
                if event.step_id != self.request.step_id:
                    raise AgentResponseIngestionIntegrityError(
                        "step ingestion marker step does not match"
                    )
            elif event.step_id is not None:
                raise AgentResponseIngestionIntegrityError(
                    "plan ingestion marker cannot reference a step"
                )

            if event.agent_id != self.request.agent_id:
                raise AgentResponseIngestionIntegrityError(
                    "ingestion marker agent does not match"
                )

        return matches

    def _already_ingested_result(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        existing: tuple[ExecutionEvent, ...],
    ) -> AgentResponseIngestionResult:
        ExecutionCheckpoint.capture(
            plan,
            journal,
            checkpoint_id=(
                f"response-ingestion-replay:"
                f"{self.request.request_hash}"
            ),
            created_at=existing[0].timestamp,
        )

        expected_plan = self._updated_plan(
            self.dispatch_result.updated_plan
        )
        if plan.to_json() != expected_plan.to_json():
            raise AgentResponseIngestionIntegrityError(
                "replayed ingestion plan differs from expected outcome"
            )

        expected_step_status = _target_step_status(
            self.response.status
        )
        step = plan.get_step(self.request.step_id)
        if step.status is not expected_step_status:
            raise AgentResponseIngestionIntegrityError(
                "replayed ingestion step status differs"
            )
        if step.assigned_agent_id != self.request.agent_id:
            raise AgentResponseIngestionIntegrityError(
                "replayed ingestion agent assignment differs"
            )

        current_plan_hash = _plan_hash(plan)
        current_seal = journal.seal()
        request_hash = self.request.request_hash
        assert request_hash is not None
        dispatch_result_hash = self.dispatch_result.result_hash
        assert dispatch_result_hash is not None

        return AgentResponseIngestionResult(
            ingestion_id=self.request.ingestion_id,
            status=AgentResponseIngestionStatus.ALREADY_INGESTED,
            request_hash=request_hash,
            dispatch_id=self.request.dispatch_id,
            dispatch_result_hash=dispatch_result_hash,
            plan_id=self.request.plan_id,
            step_id=self.request.step_id,
            agent_request_id=self.request.agent_request_id,
            agent_id=self.request.agent_id,
            response_hash=self.request.response_hash,
            response=self.response,
            appended_event_sequences=(),
            plan_before_hash=current_plan_hash,
            plan_after_hash=current_plan_hash,
            journal_before_event_count=current_seal.event_count,
            journal_after_event_count=current_seal.event_count,
            journal_before_head_hash=current_seal.head_hash,
            journal_after_head_hash=current_seal.head_hash,
            journal_before_hash=current_seal.journal_hash,
            journal_after_hash=current_seal.journal_hash,
            received_at=existing[0].timestamp,
            updated_plan=plan,
            updated_events=journal.events,
        )
