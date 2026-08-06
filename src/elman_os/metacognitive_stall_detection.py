"""Deterministic orchestration-stall detection for ELMAN-OS v0.7.

The detector consumes a hash-bound metacognitive supervision context and a
validated execution journal. It emits immutable stall windows and
MetacognitiveSupervisionFinding values. It never mutates the journal, applies a
decision, persists state, dispatches an agent, invokes an AI provider, or uses
the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .agent_contracts import canonical_json
from .execution_journal import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionJournal,
    ExecutionJournalError,
)
from .metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecisionError,
    MetacognitiveSupervisionFinding,
)


METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")

_TERMINAL_PLAN_EVENTS: Final[frozenset[ExecutionEventType]] = frozenset(
    {
        ExecutionEventType.PLAN_COMPLETED,
        ExecutionEventType.PLAN_FAILED,
    }
)
_PROGRESS_EVENTS: Final[frozenset[ExecutionEventType]] = frozenset(
    {ExecutionEventType.STEP_COMPLETED}
)


class MetacognitiveStallDetectionError(ValueError):
    """A stall-detection contract or operation is invalid."""


class MetacognitiveStallDetectionIntegrityError(
    MetacognitiveStallDetectionError
):
    """A stall-detection document or binding fails integrity verification."""


class MetacognitiveStallDetectionPolicyError(
    MetacognitiveStallDetectionError
):
    """A stall-detection policy is unsafe or internally inconsistent."""


class MetacognitiveStallDetectionStatus(StrEnum):
    CLEAR = "clear"
    STALLS_DETECTED = "stalls-detected"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveStallDetectionError(
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
        raise MetacognitiveStallDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveStallDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveStallDetectionError(f"{name} must be a boolean")
    return value


def _integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise MetacognitiveStallDetectionError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveStallDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveStallDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveStallDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveStallDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveStallDetectionError(f"{name} must be UTC")
    else:
        raise MetacognitiveStallDetectionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _identifiers(
    values: Iterable[object],
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveStallDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(
        sorted({_identifier(value, name, pattern) for value in values})
    )
    if required and not normalized:
        raise MetacognitiveStallDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _event_types(values: Iterable[object]) -> tuple[ExecutionEventType, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveStallDetectionError(
            "event_types must be an iterable"
        )
    result: list[ExecutionEventType] = []
    for value in values:
        try:
            result.append(ExecutionEventType(value))
        except (TypeError, ValueError) as exc:
            raise MetacognitiveStallDetectionError(
                "stall window contains an invalid event type"
            ) from exc
    if not result:
        raise MetacognitiveStallDetectionError(
            "event_types must contain at least one value"
        )
    return tuple(result)


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveStallDetectionPolicy:
    policy_id: str
    minimum_activity_events: int = 4
    maximum_window_events: int = 64
    minimum_sequence_span: int = 3
    high_risk_activity_events: int = 7
    critical_risk_activity_events: int = 10
    base_confidence_bp: int = 6500
    activity_confidence_increment_bp: int = 350
    include_plan_events: bool = False
    fail_closed: bool = True
    version: int = METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_id", _identifier(self.policy_id, "policy_id")
        )
        for name, minimum, maximum in (
            ("minimum_activity_events", 2, 100),
            ("maximum_window_events", 2, 1000),
            ("minimum_sequence_span", 1, 1000),
            ("high_risk_activity_events", 2, 1000),
            ("critical_risk_activity_events", 2, 1000),
            ("base_confidence_bp", 0, 10_000),
            ("activity_confidence_increment_bp", 0, 10_000),
        ):
            object.__setattr__(
                self,
                name,
                _integer(
                    getattr(self, name),
                    name,
                    minimum=minimum,
                    maximum=maximum,
                ),
            )
        if self.maximum_window_events < self.minimum_activity_events:
            raise MetacognitiveStallDetectionPolicyError(
                "maximum_window_events cannot be below minimum_activity_events"
            )
        if self.high_risk_activity_events < self.minimum_activity_events:
            raise MetacognitiveStallDetectionPolicyError(
                "high_risk_activity_events cannot be below minimum_activity_events"
            )
        if (
            self.critical_risk_activity_events
            < self.high_risk_activity_events
        ):
            raise MetacognitiveStallDetectionPolicyError(
                "critical_risk_activity_events cannot be below high_risk_activity_events"
            )
        object.__setattr__(
            self,
            "include_plan_events",
            _boolean(self.include_plan_events, "include_plan_events"),
        )
        object.__setattr__(
            self, "fail_closed", _boolean(self.fail_closed, "fail_closed")
        )
        if not self.fail_closed:
            raise MetacognitiveStallDetectionPolicyError(
                "metacognitive stall detection must fail closed"
            )
        if self.version != METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION:
            raise MetacognitiveStallDetectionError(
                "unsupported metacognitive stall-detection format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_stall_detection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "minimum_activity_events": self.minimum_activity_events,
            "maximum_window_events": self.maximum_window_events,
            "minimum_sequence_span": self.minimum_sequence_span,
            "high_risk_activity_events": self.high_risk_activity_events,
            "critical_risk_activity_events": (
                self.critical_risk_activity_events
            ),
            "base_confidence_bp": self.base_confidence_bp,
            "activity_confidence_increment_bp": (
                self.activity_confidence_increment_bp
            ),
            "include_plan_events": self.include_plan_events,
            "fail_closed": self.fail_closed,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "MetacognitiveStallDetectionPolicy":
        if data.get("record_type") != "metacognitive_stall_detection_policy":
            raise MetacognitiveStallDetectionError(
                "record_type must be metacognitive_stall_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            minimum_activity_events=data["minimum_activity_events"],
            maximum_window_events=data["maximum_window_events"],
            minimum_sequence_span=data["minimum_sequence_span"],
            high_risk_activity_events=data["high_risk_activity_events"],
            critical_risk_activity_events=data[
                "critical_risk_activity_events"
            ],
            base_confidence_bp=data["base_confidence_bp"],
            activity_confidence_increment_bp=data[
                "activity_confidence_increment_bp"
            ],
            include_plan_events=data["include_plan_events"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveStallDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveStallDetectionError(
                "stall-detection policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveStallDetectionError(
                "stall-detection policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveStallDetectionRequest:
    request_id: str
    policy_json: str
    policy_hash: str
    context_json: str
    context_hash: str
    journal_plan_id: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    journal_evidence_reference: str
    requested_by: str
    requested_at: str
    reason: str
    request_hash: str | None = None
    version: int = METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _identifier(self.request_id, "request_id")
        )
        policy = MetacognitiveStallDetectionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        try:
            context = MetacognitiveSupervisionContext.from_json(
                _text(self.context_json, "context_json")
            )
            context.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveStallDetectionIntegrityError(
                "embedded supervision context is invalid"
            ) from exc
        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != context.context_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "context_hash does not match embedded context"
            )
        object.__setattr__(self, "context_json", context.to_json())
        object.__setattr__(self, "context_hash", supplied_context_hash)

        object.__setattr__(
            self,
            "journal_plan_id",
            _identifier(self.journal_plan_id, "journal_plan_id"),
        )
        object.__setattr__(
            self,
            "journal_event_count",
            _integer(
                self.journal_event_count,
                "journal_event_count",
                minimum=0,
                maximum=10_000_000,
            ),
        )
        object.__setattr__(
            self,
            "journal_head_hash",
            _hash(self.journal_head_hash, "journal_head_hash"),
        )
        object.__setattr__(
            self, "journal_hash", _hash(self.journal_hash, "journal_hash")
        )
        object.__setattr__(
            self,
            "journal_evidence_reference",
            _identifier(
                self.journal_evidence_reference,
                "journal_evidence_reference",
            ),
        )
        if self.journal_evidence_reference not in context.evidence_references:
            raise MetacognitiveStallDetectionIntegrityError(
                "journal evidence reference is not bound to the context"
            )
        if self.journal_plan_id != context.plan_id:
            raise MetacognitiveStallDetectionIntegrityError(
                "journal plan does not match the supervision context"
            )
        if self.journal_hash != context.journal_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "journal hash does not match the supervision context"
            )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < context.observed_at:
            raise MetacognitiveStallDetectionPolicyError(
                "stall detection cannot precede context observation"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION:
            raise MetacognitiveStallDetectionError(
                "unsupported stall-detection request format version"
            )
        expected_id = (
            f"metacognitive-stall-request:{self.compute_identity_hash()}"
        )
        if self.request_id != expected_id:
            raise MetacognitiveStallDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveStallDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveStallDetectionPolicy:
        return MetacognitiveStallDetectionPolicy.from_json(self.policy_json)

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.from_json(self.context_json)

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "context_hash": self.context_hash,
            "journal_plan_id": self.journal_plan_id,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
            "journal_evidence_reference": self.journal_evidence_reference,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_stall_detection_request",
            "version": self.version,
            "request_id": self.request_id,
            "policy_json": self.policy_json,
            "context_json": self.context_json,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise MetacognitiveStallDetectionIntegrityError(
                "request_hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        policy: MetacognitiveStallDetectionPolicy,
        context: MetacognitiveSupervisionContext,
        journal: ExecutionJournal,
        journal_evidence_reference: str,
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveStallDetectionRequest":
        if not isinstance(policy, MetacognitiveStallDetectionPolicy):
            raise MetacognitiveStallDetectionError(
                "policy must be a MetacognitiveStallDetectionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveStallDetectionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveStallDetectionError(
                "journal must be an ExecutionJournal"
            )
        try:
            context.verify_hash()
            seal = journal.validate()
        except (
            MetacognitiveSupervisionDecisionError,
            ExecutionJournalError,
        ) as exc:
            raise MetacognitiveStallDetectionIntegrityError(
                "request inputs failed integrity verification"
            ) from exc
        evidence = _identifier(
            journal_evidence_reference, "journal_evidence_reference"
        )
        requested_by_value = _identifier(
            requested_by, "requested_by", _AGENT_ID
        )
        requested_at_value = _utc_timestamp(
            requested_at, "requested_at"
        )
        reason_value = _text(reason, "reason")
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "journal_plan_id": journal.plan_id,
            "journal_event_count": journal.event_count,
            "journal_head_hash": journal.head_hash,
            "journal_hash": seal.journal_hash,
            "journal_evidence_reference": evidence,
            "requested_by": requested_by_value,
            "requested_at": requested_at_value,
            "reason": reason_value,
        }
        return cls(
            request_id=(
                f"metacognitive-stall-request:{_sha256_document(identity)}"
            ),
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            journal_plan_id=journal.plan_id,
            journal_event_count=journal.event_count,
            journal_head_hash=journal.head_hash,
            journal_hash=seal.journal_hash,
            journal_evidence_reference=evidence,
            requested_by=requested_by_value,
            requested_at=requested_at_value,
            reason=reason_value,
        )

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "MetacognitiveStallDetectionRequest":
        if data.get("record_type") != "metacognitive_stall_detection_request":
            raise MetacognitiveStallDetectionError(
                "record_type must be metacognitive_stall_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveStallDetectionIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            request_id=data["request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            context_json=data["context_json"],
            context_hash=data["context_hash"],
            journal_plan_id=data["journal_plan_id"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            journal_evidence_reference=data["journal_evidence_reference"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            reason=data["reason"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveStallDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveStallDetectionError(
                "stall-detection request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveStallDetectionError(
                "stall-detection request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveStallWindow:
    window_id: str
    context_id: str
    context_hash: str
    journal_hash: str
    step_id: str
    start_sequence: int
    end_sequence: int
    activity_event_count: int
    sequence_span: int
    event_types: tuple[ExecutionEventType, ...]
    agent_ids: tuple[str, ...]
    risk_level: MetacognitiveRiskLevel
    confidence_bp: int
    evidence_references: tuple[str, ...]
    window_hash: str | None = None
    version: int = METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("window_id", "context_id"):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), name)
            )
        object.__setattr__(
            self, "context_hash", _hash(self.context_hash, "context_hash")
        )
        object.__setattr__(
            self, "journal_hash", _hash(self.journal_hash, "journal_hash")
        )
        object.__setattr__(
            self, "step_id", _identifier(self.step_id, "step_id", _STEP_ID)
        )
        for name, minimum, maximum in (
            ("start_sequence", 1, 10_000_000),
            ("end_sequence", 1, 10_000_000),
            ("activity_event_count", 2, 1000),
            ("sequence_span", 1, 10_000_000),
            ("confidence_bp", 0, 10_000),
        ):
            object.__setattr__(
                self,
                name,
                _integer(
                    getattr(self, name),
                    name,
                    minimum=minimum,
                    maximum=maximum,
                ),
            )
        if self.end_sequence < self.start_sequence:
            raise MetacognitiveStallDetectionError(
                "end_sequence cannot precede start_sequence"
            )
        if self.sequence_span != self.end_sequence - self.start_sequence + 1:
            raise MetacognitiveStallDetectionIntegrityError(
                "sequence_span does not match the journal sequence range"
            )
        if self.activity_event_count > self.sequence_span:
            raise MetacognitiveStallDetectionIntegrityError(
                "activity_event_count cannot exceed sequence_span"
            )
        normalized_types = _event_types(self.event_types)
        if len(normalized_types) != self.activity_event_count:
            raise MetacognitiveStallDetectionIntegrityError(
                "event_types length does not match activity_event_count"
            )
        if any(event_type in _PROGRESS_EVENTS for event_type in normalized_types):
            raise MetacognitiveStallDetectionPolicyError(
                "stall windows cannot contain progress events"
            )
        object.__setattr__(self, "event_types", normalized_types)
        object.__setattr__(
            self,
            "agent_ids",
            _identifiers(self.agent_ids, "agent_id", _AGENT_ID),
        )
        try:
            risk = MetacognitiveRiskLevel(self.risk_level)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveStallDetectionError(
                "stall risk level is invalid"
            ) from exc
        if risk not in {
            MetacognitiveRiskLevel.MEDIUM,
            MetacognitiveRiskLevel.HIGH,
            MetacognitiveRiskLevel.CRITICAL,
        }:
            raise MetacognitiveStallDetectionPolicyError(
                "detected stalls must be medium, high, or critical risk"
            )
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(
            self,
            "evidence_references",
            _identifiers(
                self.evidence_references,
                "evidence_reference",
                required=True,
            ),
        )
        if self.version != METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION:
            raise MetacognitiveStallDetectionError(
                "unsupported stall-window format version"
            )
        expected_id = (
            f"metacognitive-stall-window:{self.compute_identity_hash()}"
        )
        if self.window_id != expected_id:
            raise MetacognitiveStallDetectionIntegrityError(
                "window_id does not match stall-window identity"
            )
        computed = self.compute_hash()
        if self.window_hash is None:
            object.__setattr__(self, "window_hash", computed)
        else:
            supplied = _hash(self.window_hash, "window_hash")
            if supplied != computed:
                raise MetacognitiveStallDetectionIntegrityError(
                    "window_hash does not match stall-window content"
                )
            object.__setattr__(self, "window_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "context_hash": self.context_hash,
            "journal_hash": self.journal_hash,
            "step_id": self.step_id,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "activity_event_count": self.activity_event_count,
            "sequence_span": self.sequence_span,
            "event_types": [value.value for value in self.event_types],
            "agent_ids": list(self.agent_ids),
            "risk_level": self.risk_level.value,
            "confidence_bp": self.confidence_bp,
            "evidence_references": list(self.evidence_references),
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_stall_window",
            "version": self.version,
            "window_id": self.window_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.window_hash != self.compute_hash():
            raise MetacognitiveStallDetectionIntegrityError(
                "window_hash does not match stall-window content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["window_hash"] = self.window_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        context: MetacognitiveSupervisionContext,
        journal_hash: str,
        step_id: str,
        events: Sequence[ExecutionEvent],
        risk_level: MetacognitiveRiskLevel,
        confidence_bp: int,
        evidence_references: Iterable[str],
    ) -> "MetacognitiveStallWindow":
        if not events:
            raise MetacognitiveStallDetectionError(
                "stall window events cannot be empty"
            )
        context.verify_hash()
        normalized_hash = _hash(journal_hash, "journal_hash")
        normalized_step = _identifier(step_id, "step_id", _STEP_ID)
        for event in events:
            if not isinstance(event, ExecutionEvent):
                raise MetacognitiveStallDetectionError(
                    "stall window events must be ExecutionEvent values"
                )
            if event.step_id != normalized_step:
                raise MetacognitiveStallDetectionIntegrityError(
                    "stall window contains an event for another step"
                )
            if event.event_type in _PROGRESS_EVENTS:
                raise MetacognitiveStallDetectionPolicyError(
                    "stall window cannot contain a progress event"
                )
        normalized_evidence = _identifiers(
            evidence_references,
            "evidence_reference",
            required=True,
        )
        identity = {
            "context_id": context.context_id,
            "context_hash": context.context_hash,
            "journal_hash": normalized_hash,
            "step_id": normalized_step,
            "start_sequence": events[0].sequence,
            "end_sequence": events[-1].sequence,
            "activity_event_count": len(events),
            "sequence_span": events[-1].sequence - events[0].sequence + 1,
            "event_types": [event.event_type.value for event in events],
            "agent_ids": sorted(
                {
                    event.agent_id
                    for event in events
                    if event.agent_id is not None
                }
            ),
            "risk_level": MetacognitiveRiskLevel(risk_level).value,
            "confidence_bp": confidence_bp,
            "evidence_references": list(normalized_evidence),
        }
        return cls(
            window_id=(
                f"metacognitive-stall-window:{_sha256_document(identity)}"
            ),
            context_id=context.context_id,
            context_hash=context.context_hash or "",
            journal_hash=normalized_hash,
            step_id=normalized_step,
            start_sequence=events[0].sequence,
            end_sequence=events[-1].sequence,
            activity_event_count=len(events),
            sequence_span=events[-1].sequence - events[0].sequence + 1,
            event_types=tuple(event.event_type for event in events),
            agent_ids=tuple(
                sorted(
                    {
                        event.agent_id
                        for event in events
                        if event.agent_id is not None
                    }
                )
            ),
            risk_level=risk_level,
            confidence_bp=confidence_bp,
            evidence_references=normalized_evidence,
        )

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "MetacognitiveStallWindow":
        if data.get("record_type") != "metacognitive_stall_window":
            raise MetacognitiveStallDetectionError(
                "record_type must be metacognitive_stall_window"
            )
        if "window_hash" not in data:
            raise MetacognitiveStallDetectionIntegrityError(
                "serialized window is missing window_hash"
            )
        return cls(
            window_id=data["window_id"],
            context_id=data["context_id"],
            context_hash=data["context_hash"],
            journal_hash=data["journal_hash"],
            step_id=data["step_id"],
            start_sequence=data["start_sequence"],
            end_sequence=data["end_sequence"],
            activity_event_count=data["activity_event_count"],
            sequence_span=data["sequence_span"],
            event_types=tuple(data["event_types"]),
            agent_ids=tuple(data["agent_ids"]),
            risk_level=data["risk_level"],
            confidence_bp=data["confidence_bp"],
            evidence_references=tuple(data["evidence_references"]),
            window_hash=data["window_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "MetacognitiveStallWindow":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveStallDetectionError(
                "stall-window JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveStallDetectionError(
                "stall-window JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveStallDetectionRecord:
    record_id: str
    window_json: str
    window_hash: str
    finding_json: str
    finding_hash: str
    record_hash: str | None = None
    version: int = METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", _identifier(self.record_id, "record_id")
        )
        window = MetacognitiveStallWindow.from_json(
            _text(self.window_json, "window_json")
        )
        window.verify_hash()
        supplied_window_hash = _hash(self.window_hash, "window_hash")
        if supplied_window_hash != window.window_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "window_hash does not match embedded stall window"
            )
        object.__setattr__(self, "window_json", window.to_json())
        object.__setattr__(self, "window_hash", supplied_window_hash)
        try:
            finding = MetacognitiveSupervisionFinding.from_json(
                _text(self.finding_json, "finding_json")
            )
            finding.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveStallDetectionIntegrityError(
                "embedded stall finding is invalid"
            ) from exc
        supplied_finding_hash = _hash(self.finding_hash, "finding_hash")
        if supplied_finding_hash != finding.finding_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "finding_hash does not match embedded stall finding"
            )
        if (
            finding.context_id != window.context_id
            or finding.context_hash != window.context_hash
            or finding.kind is not MetacognitiveFindingKind.STALL
            or finding.risk_level is not window.risk_level
            or finding.confidence_bp != window.confidence_bp
            or finding.affected_step_ids != (window.step_id,)
            or finding.evidence_references != window.evidence_references
            or finding.summary != self.summary_for(window)
        ):
            raise MetacognitiveStallDetectionIntegrityError(
                "stall finding does not match the detected window"
            )
        object.__setattr__(self, "finding_json", finding.to_json())
        object.__setattr__(self, "finding_hash", supplied_finding_hash)
        if self.version != METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION:
            raise MetacognitiveStallDetectionError(
                "unsupported stall-detection record format version"
            )
        expected_id = (
            f"metacognitive-stall-record:{self.compute_identity_hash()}"
        )
        if self.record_id != expected_id:
            raise MetacognitiveStallDetectionIntegrityError(
                "record_id does not match stall-detection record identity"
            )
        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise MetacognitiveStallDetectionIntegrityError(
                    "record_hash does not match stall-detection record content"
                )
            object.__setattr__(self, "record_hash", supplied)

    @property
    def window(self) -> MetacognitiveStallWindow:
        return MetacognitiveStallWindow.from_json(self.window_json)

    @property
    def finding(self) -> MetacognitiveSupervisionFinding:
        return MetacognitiveSupervisionFinding.from_json(self.finding_json)

    @staticmethod
    def summary_for(window: MetacognitiveStallWindow) -> str:
        return (
            f"Detected {window.activity_event_count} non-progress activity "
            f"event(s) for step {window.step_id} between journal sequences "
            f"{window.start_sequence} and {window.end_sequence} without a "
            "subsequent step.completed event."
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "window_hash": self.window_hash,
            "finding_hash": self.finding_hash,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_stall_detection_record",
            "version": self.version,
            "record_id": self.record_id,
            "window_json": self.window_json,
            "finding_json": self.finding_json,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise MetacognitiveStallDetectionIntegrityError(
                "record_hash does not match stall-detection record content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_window(
        cls,
        *,
        context: MetacognitiveSupervisionContext,
        window: MetacognitiveStallWindow,
    ) -> "MetacognitiveStallDetectionRecord":
        window.verify_hash()
        if (
            window.context_id != context.context_id
            or window.context_hash != context.context_hash
        ):
            raise MetacognitiveStallDetectionIntegrityError(
                "stall window is not bound to the supplied context"
            )
        finding = MetacognitiveSupervisionFinding.from_context(
            context=context,
            kind=MetacognitiveFindingKind.STALL,
            risk_level=window.risk_level,
            summary=cls.summary_for(window),
            evidence_references=window.evidence_references,
            affected_step_ids=(window.step_id,),
            confidence_bp=window.confidence_bp,
        )
        identity = {
            "window_hash": window.window_hash,
            "finding_hash": finding.finding_hash,
        }
        return cls(
            record_id=(
                f"metacognitive-stall-record:{_sha256_document(identity)}"
            ),
            window_json=window.to_json(),
            window_hash=window.window_hash or "",
            finding_json=finding.to_json(),
            finding_hash=finding.finding_hash or "",
        )

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "MetacognitiveStallDetectionRecord":
        if data.get("record_type") != "metacognitive_stall_detection_record":
            raise MetacognitiveStallDetectionError(
                "record_type must be metacognitive_stall_detection_record"
            )
        if "record_hash" not in data:
            raise MetacognitiveStallDetectionIntegrityError(
                "serialized record is missing record_hash"
            )
        return cls(
            record_id=data["record_id"],
            window_json=data["window_json"],
            window_hash=data["window_hash"],
            finding_json=data["finding_json"],
            finding_hash=data["finding_hash"],
            record_hash=data["record_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveStallDetectionRecord":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveStallDetectionError(
                "stall-detection record JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveStallDetectionError(
                "stall-detection record JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveStallDetectionResult:
    result_id: str
    request_json: str
    request_hash: str
    status: MetacognitiveStallDetectionStatus
    records_json: tuple[str, ...]
    record_hashes: tuple[str, ...]
    inspected_event_count: int
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "result_id", _identifier(self.result_id, "result_id")
        )
        request = MetacognitiveStallDetectionRequest.from_json(
            _text(self.request_json, "request_json")
        )
        request.verify_hash()
        supplied_request_hash = _hash(self.request_hash, "request_hash")
        if supplied_request_hash != request.request_hash:
            raise MetacognitiveStallDetectionIntegrityError(
                "request_hash does not match embedded request"
            )
        object.__setattr__(self, "request_json", request.to_json())
        object.__setattr__(self, "request_hash", supplied_request_hash)
        try:
            status = MetacognitiveStallDetectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveStallDetectionError(
                "stall-detection result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        records = tuple(
            MetacognitiveStallDetectionRecord.from_json(
                _text(payload, "record_json")
            )
            for payload in self.records_json
        )
        for record in records:
            record.verify_hash()
            window = record.window
            if (
                window.context_id != request.context.context_id
                or window.context_hash != request.context_hash
                or window.journal_hash != request.journal_hash
            ):
                raise MetacognitiveStallDetectionIntegrityError(
                    "stall-detection record is not bound to the request"
                )
        records = tuple(
            sorted(records, key=lambda item: (item.window.step_id, item.record_id))
        )
        expected_hashes = tuple(record.record_hash or "" for record in records)
        supplied_hashes = tuple(
            _hash(value, "record_hash") for value in self.record_hashes
        )
        if supplied_hashes != expected_hashes:
            raise MetacognitiveStallDetectionIntegrityError(
                "record_hashes do not match embedded stall-detection records"
            )
        object.__setattr__(
            self,
            "records_json",
            tuple(record.to_json() for record in records),
        )
        object.__setattr__(self, "record_hashes", expected_hashes)
        if status is MetacognitiveStallDetectionStatus.CLEAR and records:
            raise MetacognitiveStallDetectionIntegrityError(
                "clear result cannot contain stall records"
            )
        if (
            status is MetacognitiveStallDetectionStatus.STALLS_DETECTED
            and not records
        ):
            raise MetacognitiveStallDetectionIntegrityError(
                "stalls-detected result requires stall records"
            )
        object.__setattr__(
            self,
            "inspected_event_count",
            _integer(
                self.inspected_event_count,
                "inspected_event_count",
                minimum=0,
                maximum=10_000_000,
            ),
        )
        if self.inspected_event_count > request.journal_event_count:
            raise MetacognitiveStallDetectionIntegrityError(
                "inspected_event_count exceeds request journal_event_count"
            )
        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at < request.requested_at:
            raise MetacognitiveStallDetectionPolicyError(
                "stall detection cannot complete before it was requested"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION:
            raise MetacognitiveStallDetectionError(
                "unsupported stall-detection result format version"
            )
        expected_id = (
            f"metacognitive-stall-result:{self.compute_identity_hash()}"
        )
        if self.result_id != expected_id:
            raise MetacognitiveStallDetectionIntegrityError(
                "result_id does not match stall-detection result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveStallDetectionIntegrityError(
                    "result_hash does not match stall-detection result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def request(self) -> MetacognitiveStallDetectionRequest:
        return MetacognitiveStallDetectionRequest.from_json(self.request_json)

    @property
    def records(self) -> tuple[MetacognitiveStallDetectionRecord, ...]:
        return tuple(
            MetacognitiveStallDetectionRecord.from_json(payload)
            for payload in self.records_json
        )

    @property
    def windows(self) -> tuple[MetacognitiveStallWindow, ...]:
        return tuple(record.window for record in self.records)

    @property
    def findings(self) -> tuple[MetacognitiveSupervisionFinding, ...]:
        return tuple(record.finding for record in self.records)

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "status": self.status.value,
            "record_hashes": list(self.record_hashes),
            "inspected_event_count": self.inspected_event_count,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_stall_detection_result",
            "version": self.version,
            "result_id": self.result_id,
            "request_json": self.request_json,
            "records_json": list(self.records_json),
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise MetacognitiveStallDetectionIntegrityError(
                "result_hash does not match stall-detection result content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["result_hash"] = self.result_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        request: MetacognitiveStallDetectionRequest,
        records: Iterable[MetacognitiveStallDetectionRecord],
        inspected_event_count: int,
        completed_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveStallDetectionResult":
        request.verify_hash()
        normalized_records = tuple(
            sorted(records, key=lambda item: (item.window.step_id, item.record_id))
        )
        status = (
            MetacognitiveStallDetectionStatus.STALLS_DETECTED
            if normalized_records
            else MetacognitiveStallDetectionStatus.CLEAR
        )
        completed = _utc_timestamp(completed_at, "completed_at")
        normalized_reason = _text(reason, "reason")
        identity = {
            "request_hash": request.request_hash,
            "status": status.value,
            "record_hashes": [
                record.record_hash for record in normalized_records
            ],
            "inspected_event_count": inspected_event_count,
            "completed_at": completed,
            "reason": normalized_reason,
        }
        return cls(
            result_id=(
                f"metacognitive-stall-result:{_sha256_document(identity)}"
            ),
            request_json=request.to_json(),
            request_hash=request.request_hash or "",
            status=status,
            records_json=tuple(
                record.to_json() for record in normalized_records
            ),
            record_hashes=tuple(
                record.record_hash or "" for record in normalized_records
            ),
            inspected_event_count=inspected_event_count,
            completed_at=completed,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "MetacognitiveStallDetectionResult":
        if data.get("record_type") != "metacognitive_stall_detection_result":
            raise MetacognitiveStallDetectionError(
                "record_type must be metacognitive_stall_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveStallDetectionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            result_id=data["result_id"],
            request_json=data["request_json"],
            request_hash=data["request_hash"],
            status=data["status"],
            records_json=tuple(data["records_json"]),
            record_hashes=tuple(data["record_hashes"]),
            inspected_event_count=data["inspected_event_count"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveStallDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveStallDetectionError(
                "stall-detection result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveStallDetectionError(
                "stall-detection result JSON must be an object"
            )
        return cls.from_dict(data)


class MetacognitiveStallDetector:
    """Read-only detector for active step windows without measurable progress."""

    @staticmethod
    def _risk(
        activity_event_count: int,
        policy: MetacognitiveStallDetectionPolicy,
    ) -> MetacognitiveRiskLevel:
        if (
            activity_event_count
            >= policy.critical_risk_activity_events
        ):
            return MetacognitiveRiskLevel.CRITICAL
        if activity_event_count >= policy.high_risk_activity_events:
            return MetacognitiveRiskLevel.HIGH
        return MetacognitiveRiskLevel.MEDIUM

    @staticmethod
    def _confidence(
        activity_event_count: int,
        policy: MetacognitiveStallDetectionPolicy,
    ) -> int:
        extra = max(
            0, activity_event_count - policy.minimum_activity_events
        )
        return min(
            10_000,
            policy.base_confidence_bp
            + extra * policy.activity_confidence_increment_bp,
        )

    @staticmethod
    def _active_windows(
        events: Sequence[ExecutionEvent],
        *,
        policy: MetacognitiveStallDetectionPolicy,
    ) -> dict[str, tuple[ExecutionEvent, ...]]:
        active: dict[str, list[ExecutionEvent]] = {}
        for event in events:
            if event.event_type in _TERMINAL_PLAN_EVENTS:
                active.clear()
                continue
            if event.step_id is None:
                if policy.include_plan_events:
                    continue
                continue
            step_id = event.step_id
            if event.event_type in _PROGRESS_EVENTS:
                active.pop(step_id, None)
                continue
            window = active.setdefault(step_id, [])
            window.append(event)
            if len(window) > policy.maximum_window_events:
                del window[: len(window) - policy.maximum_window_events]
        return {key: tuple(value) for key, value in active.items()}

    def detect(
        self,
        *,
        request: MetacognitiveStallDetectionRequest,
        journal: ExecutionJournal,
        completed_at: str | datetime,
    ) -> MetacognitiveStallDetectionResult:
        if not isinstance(request, MetacognitiveStallDetectionRequest):
            raise MetacognitiveStallDetectionError(
                "request must be a MetacognitiveStallDetectionRequest"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveStallDetectionError(
                "journal must be an ExecutionJournal"
            )
        request.verify_hash()
        context = request.context
        policy = request.policy
        context_before = context.to_json()
        journal_before = journal.to_jsonl()
        try:
            seal = journal.validate()
        except ExecutionJournalError as exc:
            raise MetacognitiveStallDetectionIntegrityError(
                "journal failed integrity verification"
            ) from exc
        if (
            journal.plan_id != request.journal_plan_id
            or journal.event_count != request.journal_event_count
            or journal.head_hash != request.journal_head_hash
            or seal.journal_hash != request.journal_hash
        ):
            raise MetacognitiveStallDetectionIntegrityError(
                "journal does not match the stall-detection request"
            )

        active = self._active_windows(journal.events, policy=policy)
        records: list[MetacognitiveStallDetectionRecord] = []
        inspected_event_count = sum(
            1 for event in journal.events if event.step_id is not None
        )
        for step_id in sorted(active):
            events = active[step_id]
            if len(events) < policy.minimum_activity_events:
                continue
            span = events[-1].sequence - events[0].sequence + 1
            if span < policy.minimum_sequence_span:
                continue
            window = MetacognitiveStallWindow.capture(
                context=context,
                journal_hash=seal.journal_hash,
                step_id=step_id,
                events=events,
                risk_level=self._risk(len(events), policy),
                confidence_bp=self._confidence(len(events), policy),
                evidence_references=(
                    request.journal_evidence_reference,
                ),
            )
            records.append(
                MetacognitiveStallDetectionRecord.from_window(
                    context=context,
                    window=window,
                )
            )

        result = MetacognitiveStallDetectionResult.create(
            request=request,
            records=records,
            inspected_event_count=inspected_event_count,
            completed_at=completed_at,
            reason=(
                "One or more deterministic orchestration stalls were detected."
                if records
                else "No deterministic orchestration stall was detected."
            ),
        )
        if context.to_json() != context_before:
            raise MetacognitiveStallDetectionIntegrityError(
                "supervision context changed during stall detection"
            )
        if journal.to_jsonl() != journal_before:
            raise MetacognitiveStallDetectionIntegrityError(
                "execution journal changed during stall detection"
            )
        return result
