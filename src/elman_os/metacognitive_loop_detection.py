"""Deterministic orchestration-loop detection for ELMAN-OS v0.7.

The detector consumes a hash-bound metacognitive supervision context and a
validated execution journal. It emits immutable loop patterns and
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


METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class MetacognitiveLoopDetectionError(ValueError):
    """A loop-detection contract or operation is invalid."""


class MetacognitiveLoopDetectionIntegrityError(
    MetacognitiveLoopDetectionError
):
    """A loop-detection document or binding fails integrity verification."""


class MetacognitiveLoopDetectionPolicyError(
    MetacognitiveLoopDetectionError
):
    """A loop-detection policy is unsafe or internally inconsistent."""


class MetacognitiveLoopDetectionStatus(StrEnum):
    CLEAR = "clear"
    LOOPS_DETECTED = "loops-detected"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveLoopDetectionError(
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
        raise MetacognitiveLoopDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveLoopDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveLoopDetectionError(
            f"{name} must be a boolean"
        )
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
        raise MetacognitiveLoopDetectionError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveLoopDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveLoopDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveLoopDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveLoopDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveLoopDetectionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveLoopDetectionError(
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
        raise MetacognitiveLoopDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(sorted({_identifier(value, name, pattern) for value in values}))
    if required and not normalized:
        raise MetacognitiveLoopDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _strings(
    values: Iterable[object],
    name: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveLoopDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(_text(value, name) for value in values)
    if required and not normalized:
        raise MetacognitiveLoopDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveLoopDetectionPolicy:
    policy_id: str
    minimum_repetitions: int = 3
    maximum_cycle_length: int = 8
    high_risk_repetitions: int = 4
    critical_risk_repetitions: int = 6
    base_confidence_bp: int = 7000
    repetition_confidence_increment_bp: int = 500
    include_plan_events: bool = False
    fail_closed: bool = True
    version: int = METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for name, minimum, maximum in (
            ("minimum_repetitions", 2, 100),
            ("maximum_cycle_length", 1, 64),
            ("high_risk_repetitions", 2, 100),
            ("critical_risk_repetitions", 2, 100),
            ("base_confidence_bp", 0, 10_000),
            ("repetition_confidence_increment_bp", 0, 10_000),
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
        if self.high_risk_repetitions < self.minimum_repetitions:
            raise MetacognitiveLoopDetectionPolicyError(
                "high_risk_repetitions cannot be below minimum_repetitions"
            )
        if self.critical_risk_repetitions < self.high_risk_repetitions:
            raise MetacognitiveLoopDetectionPolicyError(
                "critical_risk_repetitions cannot be below high_risk_repetitions"
            )
        object.__setattr__(
            self,
            "include_plan_events",
            _boolean(self.include_plan_events, "include_plan_events"),
        )
        object.__setattr__(
            self,
            "fail_closed",
            _boolean(self.fail_closed, "fail_closed"),
        )
        if not self.fail_closed:
            raise MetacognitiveLoopDetectionPolicyError(
                "metacognitive loop detection must fail closed"
            )
        if self.version != METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION:
            raise MetacognitiveLoopDetectionError(
                "unsupported metacognitive loop-detection format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_loop_detection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "minimum_repetitions": self.minimum_repetitions,
            "maximum_cycle_length": self.maximum_cycle_length,
            "high_risk_repetitions": self.high_risk_repetitions,
            "critical_risk_repetitions": self.critical_risk_repetitions,
            "base_confidence_bp": self.base_confidence_bp,
            "repetition_confidence_increment_bp": (
                self.repetition_confidence_increment_bp
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
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveLoopDetectionPolicy":
        if data.get("record_type") != "metacognitive_loop_detection_policy":
            raise MetacognitiveLoopDetectionError(
                "record_type must be metacognitive_loop_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            minimum_repetitions=data["minimum_repetitions"],
            maximum_cycle_length=data["maximum_cycle_length"],
            high_risk_repetitions=data["high_risk_repetitions"],
            critical_risk_repetitions=data["critical_risk_repetitions"],
            base_confidence_bp=data["base_confidence_bp"],
            repetition_confidence_increment_bp=data[
                "repetition_confidence_increment_bp"
            ],
            include_plan_events=data["include_plan_events"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveLoopDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-detection policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveLoopDetectionError(
                "loop-detection policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveLoopDetectionRequest:
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
    version: int = METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )
        policy = MetacognitiveLoopDetectionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        policy_hash = _hash(self.policy_hash, "policy_hash")
        if policy_hash != policy.policy_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", policy_hash)

        try:
            context = MetacognitiveSupervisionContext.from_json(
                _text(self.context_json, "context_json")
            )
            context.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveLoopDetectionIntegrityError(
                "embedded supervision context is invalid"
            ) from exc
        context_hash = _hash(self.context_hash, "context_hash")
        if context_hash != context.context_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "context_hash does not match embedded context"
            )
        object.__setattr__(self, "context_json", context.to_json())
        object.__setattr__(self, "context_hash", context_hash)

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
            self,
            "journal_hash",
            _hash(self.journal_hash, "journal_hash"),
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
            raise MetacognitiveLoopDetectionIntegrityError(
                "journal evidence reference is not bound to the context"
            )
        if self.journal_plan_id != context.plan_id:
            raise MetacognitiveLoopDetectionIntegrityError(
                "journal plan does not match the supervision context"
            )
        if self.journal_hash != context.journal_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "journal hash does not match the supervision context"
            )

        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < context.observed_at:
            raise MetacognitiveLoopDetectionPolicyError(
                "loop detection cannot precede context observation"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION:
            raise MetacognitiveLoopDetectionError(
                "unsupported loop-detection request format version"
            )

        expected_id = f"metacognitive-loop-request:{self.compute_identity_hash()}"
        if self.request_id != expected_id:
            raise MetacognitiveLoopDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveLoopDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveLoopDetectionPolicy:
        return MetacognitiveLoopDetectionPolicy.from_json(self.policy_json)

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
            "record_type": "metacognitive_loop_detection_request",
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
            raise MetacognitiveLoopDetectionIntegrityError(
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
        policy: MetacognitiveLoopDetectionPolicy,
        context: MetacognitiveSupervisionContext,
        journal: ExecutionJournal,
        journal_evidence_reference: str,
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveLoopDetectionRequest":
        if not isinstance(policy, MetacognitiveLoopDetectionPolicy):
            raise MetacognitiveLoopDetectionError(
                "policy must be a MetacognitiveLoopDetectionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveLoopDetectionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveLoopDetectionError(
                "journal must be an ExecutionJournal"
            )
        try:
            context.verify_hash()
            seal = journal.validate()
        except (MetacognitiveSupervisionDecisionError, ExecutionJournalError) as exc:
            raise MetacognitiveLoopDetectionIntegrityError(
                "request inputs failed integrity verification"
            ) from exc
        normalized_evidence = _identifier(
            journal_evidence_reference,
            "journal_evidence_reference",
        )
        normalized_requested_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_requested_at = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        normalized_reason = _text(reason, "reason")
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "journal_plan_id": journal.plan_id,
            "journal_event_count": journal.event_count,
            "journal_head_hash": journal.head_hash,
            "journal_hash": seal.journal_hash,
            "journal_evidence_reference": normalized_evidence,
            "requested_by": normalized_requested_by,
            "requested_at": normalized_requested_at,
            "reason": normalized_reason,
        }
        return cls(
            request_id=f"metacognitive-loop-request:{_sha256_document(identity)}",
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            journal_plan_id=journal.plan_id,
            journal_event_count=journal.event_count,
            journal_head_hash=journal.head_hash,
            journal_hash=seal.journal_hash,
            journal_evidence_reference=normalized_evidence,
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveLoopDetectionRequest":
        if data.get("record_type") != "metacognitive_loop_detection_request":
            raise MetacognitiveLoopDetectionError(
                "record_type must be metacognitive_loop_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveLoopDetectionIntegrityError(
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
        cls,
        payload: str,
    ) -> "MetacognitiveLoopDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-detection request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveLoopDetectionError(
                "loop-detection request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveLoopPattern:
    pattern_id: str
    context_id: str
    context_hash: str
    journal_hash: str
    start_sequence: int
    end_sequence: int
    cycle_length: int
    repetitions: int
    event_signature: tuple[str, ...]
    affected_step_ids: tuple[str, ...]
    risk_level: MetacognitiveRiskLevel
    confidence_bp: int
    evidence_references: tuple[str, ...]
    pattern_hash: str | None = None
    version: int = METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("pattern_id", "context_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "context_hash",
            _hash(self.context_hash, "context_hash"),
        )
        object.__setattr__(
            self,
            "journal_hash",
            _hash(self.journal_hash, "journal_hash"),
        )
        for name, minimum, maximum in (
            ("start_sequence", 1, 10_000_000),
            ("end_sequence", 1, 10_000_000),
            ("cycle_length", 1, 64),
            ("repetitions", 2, 100),
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
            raise MetacognitiveLoopDetectionError(
                "end_sequence cannot precede start_sequence"
            )
        if (
            self.end_sequence - self.start_sequence + 1
            != self.cycle_length * self.repetitions
        ):
            raise MetacognitiveLoopDetectionIntegrityError(
                "loop sequence span does not match cycle length and repetitions"
            )
        signature = _strings(
            self.event_signature,
            "event_signature",
            required=True,
        )
        if len(signature) != self.cycle_length:
            raise MetacognitiveLoopDetectionIntegrityError(
                "event_signature length does not match cycle_length"
            )
        object.__setattr__(self, "event_signature", signature)
        object.__setattr__(
            self,
            "affected_step_ids",
            _identifiers(
                self.affected_step_ids,
                "affected_step_id",
                _STEP_ID,
            ),
        )
        try:
            risk = MetacognitiveRiskLevel(self.risk_level)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveLoopDetectionError(
                "loop risk level is invalid"
            ) from exc
        if risk not in {
            MetacognitiveRiskLevel.MEDIUM,
            MetacognitiveRiskLevel.HIGH,
            MetacognitiveRiskLevel.CRITICAL,
        }:
            raise MetacognitiveLoopDetectionPolicyError(
                "detected loops must be medium, high, or critical risk"
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
        if self.version != METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION:
            raise MetacognitiveLoopDetectionError(
                "unsupported loop-pattern format version"
            )
        expected_id = f"metacognitive-loop-pattern:{self.compute_identity_hash()}"
        if self.pattern_id != expected_id:
            raise MetacognitiveLoopDetectionIntegrityError(
                "pattern_id does not match loop-pattern identity"
            )
        computed = self.compute_hash()
        if self.pattern_hash is None:
            object.__setattr__(self, "pattern_hash", computed)
        else:
            supplied = _hash(self.pattern_hash, "pattern_hash")
            if supplied != computed:
                raise MetacognitiveLoopDetectionIntegrityError(
                    "pattern_hash does not match loop-pattern content"
                )
            object.__setattr__(self, "pattern_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "context_hash": self.context_hash,
            "journal_hash": self.journal_hash,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "cycle_length": self.cycle_length,
            "repetitions": self.repetitions,
            "event_signature": list(self.event_signature),
            "affected_step_ids": list(self.affected_step_ids),
            "risk_level": self.risk_level.value,
            "confidence_bp": self.confidence_bp,
            "evidence_references": list(self.evidence_references),
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_loop_pattern",
            "version": self.version,
            "pattern_id": self.pattern_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.pattern_hash != self.compute_hash():
            raise MetacognitiveLoopDetectionIntegrityError(
                "pattern_hash does not match loop-pattern content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["pattern_hash"] = self.pattern_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        context: MetacognitiveSupervisionContext,
        journal_hash: str,
        start_sequence: int,
        end_sequence: int,
        cycle_length: int,
        repetitions: int,
        event_signature: Sequence[str],
        affected_step_ids: Iterable[str],
        risk_level: MetacognitiveRiskLevel,
        confidence_bp: int,
        evidence_references: Iterable[str],
    ) -> "MetacognitiveLoopPattern":
        context.verify_hash()
        normalized_hash = _hash(journal_hash, "journal_hash")
        normalized_signature = _strings(
            event_signature,
            "event_signature",
            required=True,
        )
        normalized_steps = _identifiers(
            affected_step_ids,
            "affected_step_id",
            _STEP_ID,
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
            "start_sequence": start_sequence,
            "end_sequence": end_sequence,
            "cycle_length": cycle_length,
            "repetitions": repetitions,
            "event_signature": list(normalized_signature),
            "affected_step_ids": list(normalized_steps),
            "risk_level": MetacognitiveRiskLevel(risk_level).value,
            "confidence_bp": confidence_bp,
            "evidence_references": list(normalized_evidence),
        }
        return cls(
            pattern_id=f"metacognitive-loop-pattern:{_sha256_document(identity)}",
            context_id=context.context_id,
            context_hash=context.context_hash or "",
            journal_hash=normalized_hash,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            cycle_length=cycle_length,
            repetitions=repetitions,
            event_signature=normalized_signature,
            affected_step_ids=normalized_steps,
            risk_level=risk_level,
            confidence_bp=confidence_bp,
            evidence_references=normalized_evidence,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveLoopPattern":
        if data.get("record_type") != "metacognitive_loop_pattern":
            raise MetacognitiveLoopDetectionError(
                "record_type must be metacognitive_loop_pattern"
            )
        if "pattern_hash" not in data:
            raise MetacognitiveLoopDetectionIntegrityError(
                "serialized pattern is missing pattern_hash"
            )
        return cls(
            pattern_id=data["pattern_id"],
            context_id=data["context_id"],
            context_hash=data["context_hash"],
            journal_hash=data["journal_hash"],
            start_sequence=data["start_sequence"],
            end_sequence=data["end_sequence"],
            cycle_length=data["cycle_length"],
            repetitions=data["repetitions"],
            event_signature=tuple(data["event_signature"]),
            affected_step_ids=tuple(data["affected_step_ids"]),
            risk_level=data["risk_level"],
            confidence_bp=data["confidence_bp"],
            evidence_references=tuple(data["evidence_references"]),
            pattern_hash=data["pattern_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveLoopPattern":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-pattern JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveLoopDetectionError(
                "loop-pattern JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveLoopDetectionRecord:
    record_id: str
    pattern_json: str
    pattern_hash: str
    finding_json: str
    finding_hash: str
    record_hash: str | None = None
    version: int = METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            _identifier(self.record_id, "record_id"),
        )
        pattern = MetacognitiveLoopPattern.from_json(
            _text(self.pattern_json, "pattern_json")
        )
        pattern.verify_hash()
        pattern_hash = _hash(self.pattern_hash, "pattern_hash")
        if pattern_hash != pattern.pattern_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "pattern_hash does not match embedded loop pattern"
            )
        object.__setattr__(self, "pattern_json", pattern.to_json())
        object.__setattr__(self, "pattern_hash", pattern_hash)

        try:
            finding = MetacognitiveSupervisionFinding.from_json(
                _text(self.finding_json, "finding_json")
            )
            finding.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveLoopDetectionIntegrityError(
                "embedded loop finding is invalid"
            ) from exc
        finding_hash = _hash(self.finding_hash, "finding_hash")
        if finding_hash != finding.finding_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "finding_hash does not match embedded loop finding"
            )
        if (
            finding.context_id != pattern.context_id
            or finding.context_hash != pattern.context_hash
            or finding.kind is not MetacognitiveFindingKind.LOOP
            or finding.risk_level is not pattern.risk_level
            or finding.confidence_bp != pattern.confidence_bp
            or finding.affected_step_ids != pattern.affected_step_ids
            or finding.evidence_references != pattern.evidence_references
        ):
            raise MetacognitiveLoopDetectionIntegrityError(
                "loop finding does not match the detected pattern"
            )
        expected_summary = self.summary_for(pattern)
        if finding.summary != expected_summary:
            raise MetacognitiveLoopDetectionIntegrityError(
                "loop finding summary does not match the detected pattern"
            )
        object.__setattr__(self, "finding_json", finding.to_json())
        object.__setattr__(self, "finding_hash", finding_hash)

        if self.version != METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION:
            raise MetacognitiveLoopDetectionError(
                "unsupported loop-detection record format version"
            )
        expected_id = f"metacognitive-loop-record:{self.compute_identity_hash()}"
        if self.record_id != expected_id:
            raise MetacognitiveLoopDetectionIntegrityError(
                "record_id does not match loop-detection record identity"
            )
        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise MetacognitiveLoopDetectionIntegrityError(
                    "record_hash does not match loop-detection record content"
                )
            object.__setattr__(self, "record_hash", supplied)

    @property
    def pattern(self) -> MetacognitiveLoopPattern:
        return MetacognitiveLoopPattern.from_json(self.pattern_json)

    @property
    def finding(self) -> MetacognitiveSupervisionFinding:
        return MetacognitiveSupervisionFinding.from_json(self.finding_json)

    @staticmethod
    def summary_for(pattern: MetacognitiveLoopPattern) -> str:
        return (
            "Detected a contiguous orchestration cycle of "
            f"{pattern.cycle_length} event(s) repeated "
            f"{pattern.repetitions} time(s) between journal sequences "
            f"{pattern.start_sequence} and {pattern.end_sequence}."
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "pattern_hash": self.pattern_hash,
            "finding_hash": self.finding_hash,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_loop_detection_record",
            "version": self.version,
            "record_id": self.record_id,
            "pattern_json": self.pattern_json,
            "finding_json": self.finding_json,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise MetacognitiveLoopDetectionIntegrityError(
                "record_hash does not match loop-detection record content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_pattern(
        cls,
        *,
        context: MetacognitiveSupervisionContext,
        pattern: MetacognitiveLoopPattern,
    ) -> "MetacognitiveLoopDetectionRecord":
        pattern.verify_hash()
        if (
            pattern.context_id != context.context_id
            or pattern.context_hash != context.context_hash
        ):
            raise MetacognitiveLoopDetectionIntegrityError(
                "loop pattern is not bound to the supplied context"
            )
        finding = MetacognitiveSupervisionFinding.from_context(
            context=context,
            kind=MetacognitiveFindingKind.LOOP,
            risk_level=pattern.risk_level,
            summary=cls.summary_for(pattern),
            evidence_references=pattern.evidence_references,
            affected_step_ids=pattern.affected_step_ids,
            confidence_bp=pattern.confidence_bp,
        )
        identity = {
            "pattern_hash": pattern.pattern_hash,
            "finding_hash": finding.finding_hash,
        }
        return cls(
            record_id=f"metacognitive-loop-record:{_sha256_document(identity)}",
            pattern_json=pattern.to_json(),
            pattern_hash=pattern.pattern_hash or "",
            finding_json=finding.to_json(),
            finding_hash=finding.finding_hash or "",
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveLoopDetectionRecord":
        if data.get("record_type") != "metacognitive_loop_detection_record":
            raise MetacognitiveLoopDetectionError(
                "record_type must be metacognitive_loop_detection_record"
            )
        if "record_hash" not in data:
            raise MetacognitiveLoopDetectionIntegrityError(
                "serialized record is missing record_hash"
            )
        return cls(
            record_id=data["record_id"],
            pattern_json=data["pattern_json"],
            pattern_hash=data["pattern_hash"],
            finding_json=data["finding_json"],
            finding_hash=data["finding_hash"],
            record_hash=data["record_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveLoopDetectionRecord":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-detection record JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveLoopDetectionError(
                "loop-detection record JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveLoopDetectionResult:
    result_id: str
    request_json: str
    request_hash: str
    status: MetacognitiveLoopDetectionStatus
    records_json: tuple[str, ...]
    record_hashes: tuple[str, ...]
    inspected_event_count: int
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "result_id",
            _identifier(self.result_id, "result_id"),
        )
        request = MetacognitiveLoopDetectionRequest.from_json(
            _text(self.request_json, "request_json")
        )
        request.verify_hash()
        request_hash = _hash(self.request_hash, "request_hash")
        if request_hash != request.request_hash:
            raise MetacognitiveLoopDetectionIntegrityError(
                "request_hash does not match embedded request"
            )
        object.__setattr__(self, "request_json", request.to_json())
        object.__setattr__(self, "request_hash", request_hash)

        try:
            status = MetacognitiveLoopDetectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-detection result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        records = tuple(
            MetacognitiveLoopDetectionRecord.from_json(
                _text(payload, "record_json")
            )
            for payload in self.records_json
        )
        for record in records:
            record.verify_hash()
            pattern = record.pattern
            if (
                pattern.context_id != request.context.context_id
                or pattern.context_hash != request.context_hash
                or pattern.journal_hash != request.journal_hash
            ):
                raise MetacognitiveLoopDetectionIntegrityError(
                    "loop-detection record is not bound to the request"
                )
        records = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.pattern.start_sequence,
                    item.pattern.cycle_length,
                    item.record_id,
                ),
            )
        )
        hashes = tuple(record.record_hash or "" for record in records)
        supplied_hashes = tuple(
            _hash(value, "record_hash") for value in self.record_hashes
        )
        if supplied_hashes != hashes:
            raise MetacognitiveLoopDetectionIntegrityError(
                "record_hashes do not match embedded loop-detection records"
            )
        object.__setattr__(
            self,
            "records_json",
            tuple(record.to_json() for record in records),
        )
        object.__setattr__(self, "record_hashes", hashes)

        if status is MetacognitiveLoopDetectionStatus.CLEAR and records:
            raise MetacognitiveLoopDetectionIntegrityError(
                "clear result cannot contain loop records"
            )
        if status is MetacognitiveLoopDetectionStatus.LOOPS_DETECTED and not records:
            raise MetacognitiveLoopDetectionIntegrityError(
                "loops-detected result requires loop records"
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
            raise MetacognitiveLoopDetectionIntegrityError(
                "inspected_event_count exceeds request journal_event_count"
            )
        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at < request.requested_at:
            raise MetacognitiveLoopDetectionPolicyError(
                "loop detection cannot complete before it was requested"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_LOOP_DETECTION_FORMAT_VERSION:
            raise MetacognitiveLoopDetectionError(
                "unsupported loop-detection result format version"
            )

        expected_id = f"metacognitive-loop-result:{self.compute_identity_hash()}"
        if self.result_id != expected_id:
            raise MetacognitiveLoopDetectionIntegrityError(
                "result_id does not match loop-detection result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveLoopDetectionIntegrityError(
                    "result_hash does not match loop-detection result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def request(self) -> MetacognitiveLoopDetectionRequest:
        return MetacognitiveLoopDetectionRequest.from_json(self.request_json)

    @property
    def records(self) -> tuple[MetacognitiveLoopDetectionRecord, ...]:
        return tuple(
            MetacognitiveLoopDetectionRecord.from_json(payload)
            for payload in self.records_json
        )

    @property
    def patterns(self) -> tuple[MetacognitiveLoopPattern, ...]:
        return tuple(record.pattern for record in self.records)

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
            "record_type": "metacognitive_loop_detection_result",
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
            raise MetacognitiveLoopDetectionIntegrityError(
                "result_hash does not match loop-detection result content"
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
        request: MetacognitiveLoopDetectionRequest,
        records: Iterable[MetacognitiveLoopDetectionRecord],
        inspected_event_count: int,
        completed_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveLoopDetectionResult":
        request.verify_hash()
        normalized_records = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.pattern.start_sequence,
                    item.pattern.cycle_length,
                    item.record_id,
                ),
            )
        )
        status = (
            MetacognitiveLoopDetectionStatus.LOOPS_DETECTED
            if normalized_records
            else MetacognitiveLoopDetectionStatus.CLEAR
        )
        normalized_completed_at = _utc_timestamp(
            completed_at,
            "completed_at",
        )
        normalized_reason = _text(reason, "reason")
        identity = {
            "request_hash": request.request_hash,
            "status": status.value,
            "record_hashes": [
                record.record_hash for record in normalized_records
            ],
            "inspected_event_count": inspected_event_count,
            "completed_at": normalized_completed_at,
            "reason": normalized_reason,
        }
        return cls(
            result_id=f"metacognitive-loop-result:{_sha256_document(identity)}",
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
            completed_at=normalized_completed_at,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveLoopDetectionResult":
        if data.get("record_type") != "metacognitive_loop_detection_result":
            raise MetacognitiveLoopDetectionError(
                "record_type must be metacognitive_loop_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveLoopDetectionIntegrityError(
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
        cls,
        payload: str,
    ) -> "MetacognitiveLoopDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveLoopDetectionError(
                "loop-detection result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveLoopDetectionError(
                "loop-detection result JSON must be an object"
            )
        return cls.from_dict(data)


class MetacognitiveLoopDetector:
    """Read-only deterministic detector for contiguous journal event cycles."""

    @staticmethod
    def _signature(event: ExecutionEvent) -> str:
        return "|".join(
            (
                event.event_type.value,
                event.step_id or "-",
                event.agent_id or "-",
            )
        )

    @staticmethod
    def _segments(
        events: Sequence[ExecutionEvent],
        *,
        include_plan_events: bool,
    ) -> tuple[tuple[ExecutionEvent, ...], ...]:
        if include_plan_events:
            return (tuple(events),) if events else ()
        segments: list[tuple[ExecutionEvent, ...]] = []
        current: list[ExecutionEvent] = []
        for event in events:
            if event.step_id is None:
                if current:
                    segments.append(tuple(current))
                    current = []
                continue
            current.append(event)
        if current:
            segments.append(tuple(current))
        return tuple(segments)

    @staticmethod
    def _best_candidate(
        segment: Sequence[ExecutionEvent],
        cursor: int,
        policy: MetacognitiveLoopDetectionPolicy,
    ) -> tuple[int, int] | None:
        remaining = len(segment) - cursor
        max_cycle = min(
            policy.maximum_cycle_length,
            remaining // policy.minimum_repetitions,
        )
        best: tuple[int, int, int, tuple[str, ...]] | None = None
        signatures = [
            MetacognitiveLoopDetector._signature(event)
            for event in segment
        ]
        for cycle_length in range(1, max_cycle + 1):
            cycle = tuple(signatures[cursor : cursor + cycle_length])
            repetitions = 1
            while True:
                start = cursor + repetitions * cycle_length
                end = start + cycle_length
                if end > len(signatures):
                    break
                if tuple(signatures[start:end]) != cycle:
                    break
                repetitions += 1
            if repetitions < policy.minimum_repetitions:
                continue
            coverage = cycle_length * repetitions
            candidate = (coverage, repetitions, -cycle_length, cycle)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            return None
        coverage, repetitions, negative_cycle_length, _ = best
        return -negative_cycle_length, repetitions

    @staticmethod
    def _risk(
        repetitions: int,
        policy: MetacognitiveLoopDetectionPolicy,
    ) -> MetacognitiveRiskLevel:
        if repetitions >= policy.critical_risk_repetitions:
            return MetacognitiveRiskLevel.CRITICAL
        if repetitions >= policy.high_risk_repetitions:
            return MetacognitiveRiskLevel.HIGH
        return MetacognitiveRiskLevel.MEDIUM

    @staticmethod
    def _confidence(
        repetitions: int,
        policy: MetacognitiveLoopDetectionPolicy,
    ) -> int:
        extra = max(0, repetitions - policy.minimum_repetitions)
        return min(
            10_000,
            policy.base_confidence_bp
            + extra * policy.repetition_confidence_increment_bp,
        )

    def detect(
        self,
        *,
        request: MetacognitiveLoopDetectionRequest,
        journal: ExecutionJournal,
        completed_at: str | datetime,
    ) -> MetacognitiveLoopDetectionResult:
        if not isinstance(request, MetacognitiveLoopDetectionRequest):
            raise MetacognitiveLoopDetectionError(
                "request must be a MetacognitiveLoopDetectionRequest"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveLoopDetectionError(
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
            raise MetacognitiveLoopDetectionIntegrityError(
                "journal failed integrity verification"
            ) from exc
        if (
            journal.plan_id != request.journal_plan_id
            or journal.event_count != request.journal_event_count
            or journal.head_hash != request.journal_head_hash
            or seal.journal_hash != request.journal_hash
        ):
            raise MetacognitiveLoopDetectionIntegrityError(
                "journal does not match the loop-detection request"
            )

        records: list[MetacognitiveLoopDetectionRecord] = []
        inspected_event_count = 0
        for segment in self._segments(
            journal.events,
            include_plan_events=policy.include_plan_events,
        ):
            inspected_event_count += len(segment)
            cursor = 0
            while cursor < len(segment):
                candidate = self._best_candidate(segment, cursor, policy)
                if candidate is None:
                    cursor += 1
                    continue
                cycle_length, repetitions = candidate
                coverage = cycle_length * repetitions
                covered = tuple(segment[cursor : cursor + coverage])
                cycle = tuple(covered[:cycle_length])
                affected_steps = tuple(
                    sorted(
                        {
                            event.step_id
                            for event in covered
                            if event.step_id is not None
                        }
                    )
                )
                pattern = MetacognitiveLoopPattern.capture(
                    context=context,
                    journal_hash=seal.journal_hash,
                    start_sequence=covered[0].sequence,
                    end_sequence=covered[-1].sequence,
                    cycle_length=cycle_length,
                    repetitions=repetitions,
                    event_signature=tuple(
                        self._signature(event) for event in cycle
                    ),
                    affected_step_ids=affected_steps,
                    risk_level=self._risk(repetitions, policy),
                    confidence_bp=self._confidence(repetitions, policy),
                    evidence_references=(
                        request.journal_evidence_reference,
                    ),
                )
                records.append(
                    MetacognitiveLoopDetectionRecord.from_pattern(
                        context=context,
                        pattern=pattern,
                    )
                )
                cursor += coverage

        result = MetacognitiveLoopDetectionResult.create(
            request=request,
            records=records,
            inspected_event_count=inspected_event_count,
            completed_at=completed_at,
            reason=(
                "One or more deterministic orchestration loops were detected."
                if records
                else "No deterministic orchestration loop was detected."
            ),
        )

        if context.to_json() != context_before:
            raise MetacognitiveLoopDetectionIntegrityError(
                "supervision context changed during loop detection"
            )
        if journal.to_jsonl() != journal_before:
            raise MetacognitiveLoopDetectionIntegrityError(
                "execution journal changed during loop detection"
            )
        return result
