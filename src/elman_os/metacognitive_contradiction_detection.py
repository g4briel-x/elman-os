"""Deterministic contradiction detection for ELMAN-OS v0.7.

The detector consumes a hash-bound metacognitive supervision context and a
validated execution journal. It emits immutable contradiction records and
MetacognitiveSupervisionFinding values. Contradictions are detected only from
explicit, scope-bound assertion envelopes or from an event that regresses a
step after it has completed.

The module is deliberately read-only. It does not mutate an execution plan or
journal, apply a supervision decision, persist state, dispatch an agent, invoke
an AI provider, or perform network access.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from .agent_contracts import FrozenJson, canonical_json
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


METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{1,191}$")
_PREDICATE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{1,191}$")


class MetacognitiveContradictionDetectionError(ValueError):
    """A contradiction-detection contract or operation is invalid."""


class MetacognitiveContradictionDetectionIntegrityError(
    MetacognitiveContradictionDetectionError
):
    """A contradiction-detection document or binding fails verification."""


class MetacognitiveContradictionDetectionPolicyError(
    MetacognitiveContradictionDetectionError
):
    """A contradiction-detection policy is unsafe or inconsistent."""


class MetacognitiveContradictionDetectionStatus(StrEnum):
    CLEAR = "clear"
    CONTRADICTIONS_DETECTED = "contradictions-detected"


class MetacognitiveContradictionKind(StrEnum):
    ASSERTION_CONFLICT = "assertion-conflict"
    COMPLETED_STEP_REGRESSION = "completed-step-regression"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveContradictionDetectionError(
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
        raise MetacognitiveContradictionDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveContradictionDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveContradictionDetectionError(
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
        raise MetacognitiveContradictionDetectionError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveContradictionDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveContradictionDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveContradictionDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveContradictionDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveContradictionDetectionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveContradictionDetectionError(
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
        raise MetacognitiveContradictionDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(
        sorted({_identifier(value, name, pattern) for value in values})
    )
    if required and not normalized:
        raise MetacognitiveContradictionDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _hashes(
    values: Iterable[object],
    name: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveContradictionDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(sorted({_hash(value, name) for value in values}))
    if required and not normalized:
        raise MetacognitiveContradictionDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _positive_integers(
    values: Iterable[object],
    name: str,
    *,
    required: bool = False,
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveContradictionDetectionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(
        sorted(
            {
                _integer(value, name, minimum=1, maximum=10_000_000)
                for value in values
            }
        )
    )
    if required and not normalized:
        raise MetacognitiveContradictionDetectionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _thaw_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MetacognitiveContradictionDetectionError(
                "assertion value contains a non-finite number"
            )
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise MetacognitiveContradictionDetectionError(
                "assertion value contains a non-string key"
            )
        return {
            key: _thaw_json(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    raise MetacognitiveContradictionDetectionError(
        f"assertion value contains non-JSON type {type(value).__name__}"
    )


def _freeze_json(value: Any, path: str = "value") -> FrozenJson:
    thawed = _thaw_json(value)
    if thawed is None or isinstance(thawed, (str, bool, int, float)):
        return thawed
    if isinstance(thawed, dict):
        return MappingProxyType(
            {
                key: _freeze_json(item, f"{path}.{key}")
                for key, item in sorted(thawed.items())
            }
        )
    if isinstance(thawed, list):
        return tuple(
            _freeze_json(item, f"{path}[{index}]")
            for index, item in enumerate(thawed)
        )
    raise MetacognitiveContradictionDetectionError(
        f"{path} is not JSON-compatible"
    )


def _canonical_value_json(value: Any) -> str:
    return canonical_json(_thaw_json(value))


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveContradictionDetectionPolicy:
    policy_id: str
    assertion_payload_key: str = "metacognitive_assertions"
    maximum_assertions_per_event: int = 64
    high_risk_distinct_values: int = 3
    critical_risk_distinct_values: int = 4
    base_confidence_bp: int = 7000
    source_confidence_increment_bp: int = 500
    value_confidence_increment_bp: int = 250
    include_assertion_conflicts: bool = True
    include_completed_step_regressions: bool = True
    fail_closed: bool = True
    version: int = METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_id", _identifier(self.policy_id, "policy_id")
        )
        object.__setattr__(
            self,
            "assertion_payload_key",
            _identifier(
                self.assertion_payload_key,
                "assertion_payload_key",
                _PREDICATE,
            ),
        )
        for name, minimum, maximum in (
            ("maximum_assertions_per_event", 1, 1000),
            ("high_risk_distinct_values", 2, 100),
            ("critical_risk_distinct_values", 2, 100),
            ("base_confidence_bp", 0, 10_000),
            ("source_confidence_increment_bp", 0, 10_000),
            ("value_confidence_increment_bp", 0, 10_000),
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
        if (
            self.critical_risk_distinct_values
            < self.high_risk_distinct_values
        ):
            raise MetacognitiveContradictionDetectionPolicyError(
                "critical_risk_distinct_values cannot be below "
                "high_risk_distinct_values"
            )
        for name in (
            "include_assertion_conflicts",
            "include_completed_step_regressions",
            "fail_closed",
        ):
            object.__setattr__(
                self, name, _boolean(getattr(self, name), name)
            )
        if not (
            self.include_assertion_conflicts
            or self.include_completed_step_regressions
        ):
            raise MetacognitiveContradictionDetectionPolicyError(
                "at least one contradiction detector must be enabled"
            )
        if not self.fail_closed:
            raise MetacognitiveContradictionDetectionPolicyError(
                "metacognitive contradiction detection must fail closed"
            )
        if (
            self.version
            != METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveContradictionDetectionError(
                "unsupported metacognitive contradiction-detection "
                "format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_contradiction_detection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "assertion_payload_key": self.assertion_payload_key,
            "maximum_assertions_per_event": self.maximum_assertions_per_event,
            "high_risk_distinct_values": self.high_risk_distinct_values,
            "critical_risk_distinct_values": (
                self.critical_risk_distinct_values
            ),
            "base_confidence_bp": self.base_confidence_bp,
            "source_confidence_increment_bp": (
                self.source_confidence_increment_bp
            ),
            "value_confidence_increment_bp": (
                self.value_confidence_increment_bp
            ),
            "include_assertion_conflicts": self.include_assertion_conflicts,
            "include_completed_step_regressions": (
                self.include_completed_step_regressions
            ),
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
    ) -> "MetacognitiveContradictionDetectionPolicy":
        if (
            data.get("record_type")
            != "metacognitive_contradiction_detection_policy"
        ):
            raise MetacognitiveContradictionDetectionError(
                "record_type must be "
                "metacognitive_contradiction_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            assertion_payload_key=data["assertion_payload_key"],
            maximum_assertions_per_event=data[
                "maximum_assertions_per_event"
            ],
            high_risk_distinct_values=data[
                "high_risk_distinct_values"
            ],
            critical_risk_distinct_values=data[
                "critical_risk_distinct_values"
            ],
            base_confidence_bp=data["base_confidence_bp"],
            source_confidence_increment_bp=data[
                "source_confidence_increment_bp"
            ],
            value_confidence_increment_bp=data[
                "value_confidence_increment_bp"
            ],
            include_assertion_conflicts=data[
                "include_assertion_conflicts"
            ],
            include_completed_step_regressions=data[
                "include_completed_step_regressions"
            ],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveContradictionDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveAssertion:
    assertion_id: str
    scope: str
    subject: str
    predicate: str
    value_json: str
    source_event_sequence: int
    source_event_hash: str
    step_id: str | None = None
    agent_id: str | None = None
    assertion_hash: str | None = None
    version: int = METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assertion_id",
            _identifier(self.assertion_id, "assertion_id"),
        )
        object.__setattr__(
            self, "scope", _identifier(self.scope, "scope", _SCOPE)
        )
        object.__setattr__(
            self, "subject", _identifier(self.subject, "subject", _SUBJECT)
        )
        object.__setattr__(
            self,
            "predicate",
            _identifier(self.predicate, "predicate", _PREDICATE),
        )
        raw_value_json = _text(self.value_json, "value_json")
        try:
            value = json.loads(raw_value_json)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "value_json is invalid"
            ) from exc
        canonical_value = canonical_json(_thaw_json(value))
        object.__setattr__(self, "value_json", canonical_value)
        object.__setattr__(
            self,
            "source_event_sequence",
            _integer(
                self.source_event_sequence,
                "source_event_sequence",
                minimum=1,
                maximum=10_000_000,
            ),
        )
        object.__setattr__(
            self,
            "source_event_hash",
            _hash(self.source_event_hash, "source_event_hash"),
        )
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
        if (
            self.version
            != METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveContradictionDetectionError(
                "unsupported metacognitive assertion format version"
            )
        expected_id = (
            f"metacognitive-assertion:{self.compute_identity_hash()}"
        )
        if self.assertion_id != expected_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "assertion_id does not match assertion identity"
            )
        computed = self.compute_hash()
        if self.assertion_hash is None:
            object.__setattr__(self, "assertion_hash", computed)
        else:
            supplied = _hash(self.assertion_hash, "assertion_hash")
            if supplied != computed:
                raise MetacognitiveContradictionDetectionIntegrityError(
                    "assertion_hash does not match assertion content"
                )
            object.__setattr__(self, "assertion_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "predicate": self.predicate,
            "value_json": self.value_json,
            "source_event_sequence": self.source_event_sequence,
            "source_event_hash": self.source_event_hash,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_assertion",
            "version": self.version,
            "assertion_id": self.assertion_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.assertion_hash != self.compute_hash():
            raise MetacognitiveContradictionDetectionIntegrityError(
                "assertion_hash does not match assertion content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["assertion_hash"] = self.assertion_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        scope: str,
        subject: str,
        predicate: str,
        value: Any,
        event: ExecutionEvent,
        step_id: str | None = None,
    ) -> "MetacognitiveAssertion":
        if not isinstance(event, ExecutionEvent):
            raise MetacognitiveContradictionDetectionError(
                "event must be an ExecutionEvent"
            )
        event.verify_hash()
        normalized_scope = _identifier(scope, "scope", _SCOPE)
        normalized_subject = _identifier(subject, "subject", _SUBJECT)
        normalized_predicate = _identifier(
            predicate, "predicate", _PREDICATE
        )
        value_json = _canonical_value_json(value)
        normalized_step_id = step_id
        if normalized_step_id is not None:
            normalized_step_id = _identifier(
                normalized_step_id, "step_id", _STEP_ID
            )
        if (
            event.step_id is not None
            and normalized_step_id is not None
            and event.step_id != normalized_step_id
        ):
            raise MetacognitiveContradictionDetectionIntegrityError(
                "assertion step_id does not match its source event"
            )
        effective_step_id = normalized_step_id or event.step_id
        identity = {
            "scope": normalized_scope,
            "subject": normalized_subject,
            "predicate": normalized_predicate,
            "value_json": value_json,
            "source_event_sequence": event.sequence,
            "source_event_hash": event.event_hash,
            "step_id": effective_step_id,
            "agent_id": event.agent_id,
        }
        return cls(
            assertion_id=(
                f"metacognitive-assertion:{_sha256_document(identity)}"
            ),
            scope=normalized_scope,
            subject=normalized_subject,
            predicate=normalized_predicate,
            value_json=value_json,
            source_event_sequence=event.sequence,
            source_event_hash=event.event_hash or "",
            step_id=effective_step_id,
            agent_id=event.agent_id,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveAssertion":
        if data.get("record_type") != "metacognitive_assertion":
            raise MetacognitiveContradictionDetectionError(
                "record_type must be metacognitive_assertion"
            )
        if "assertion_hash" not in data:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "serialized assertion is missing assertion_hash"
            )
        return cls(
            assertion_id=data["assertion_id"],
            scope=data["scope"],
            subject=data["subject"],
            predicate=data["predicate"],
            value_json=data["value_json"],
            source_event_sequence=data["source_event_sequence"],
            source_event_hash=data["source_event_hash"],
            step_id=data.get("step_id"),
            agent_id=data.get("agent_id"),
            assertion_hash=data["assertion_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "MetacognitiveAssertion":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "metacognitive assertion JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveContradictionDetectionError(
                "metacognitive assertion JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveContradiction:
    contradiction_id: str
    kind: MetacognitiveContradictionKind
    scope: str
    subject: str
    predicate: str
    values_json: tuple[str, ...]
    source_event_sequences: tuple[int, ...]
    source_event_hashes: tuple[str, ...]
    assertion_hashes: tuple[str, ...]
    affected_step_ids: tuple[str, ...]
    risk_level: MetacognitiveRiskLevel
    confidence_bp: int
    contradiction_hash: str | None = None
    version: int = METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contradiction_id",
            _identifier(self.contradiction_id, "contradiction_id"),
        )
        try:
            kind = MetacognitiveContradictionKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveContradictionDetectionError(
                "contradiction kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "scope", _identifier(self.scope, "scope", _SCOPE)
        )
        object.__setattr__(
            self, "subject", _identifier(self.subject, "subject", _SUBJECT)
        )
        object.__setattr__(
            self,
            "predicate",
            _identifier(self.predicate, "predicate", _PREDICATE),
        )
        values: list[str] = []
        for raw in self.values_json:
            text = _text(raw, "value_json")
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MetacognitiveContradictionDetectionError(
                    "contradiction value_json is invalid"
                ) from exc
            values.append(canonical_json(_thaw_json(decoded)))
        normalized_values = tuple(sorted(set(values)))
        if len(normalized_values) < 2:
            raise MetacognitiveContradictionDetectionError(
                "contradiction must contain at least two distinct values"
            )
        object.__setattr__(self, "values_json", normalized_values)
        object.__setattr__(
            self,
            "source_event_sequences",
            _positive_integers(
                self.source_event_sequences,
                "source_event_sequence",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "source_event_hashes",
            _hashes(
                self.source_event_hashes,
                "source_event_hash",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "assertion_hashes",
            _hashes(self.assertion_hashes, "assertion_hash"),
        )
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
            raise MetacognitiveContradictionDetectionError(
                "metacognitive risk level is invalid"
            ) from exc
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(
            self,
            "confidence_bp",
            _integer(
                self.confidence_bp,
                "confidence_bp",
                minimum=0,
                maximum=10_000,
            ),
        )
        if (
            self.version
            != METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveContradictionDetectionError(
                "unsupported metacognitive contradiction format version"
            )
        expected_id = (
            f"metacognitive-contradiction:{self.compute_identity_hash()}"
        )
        if self.contradiction_id != expected_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "contradiction_id does not match contradiction identity"
            )
        computed = self.compute_hash()
        if self.contradiction_hash is None:
            object.__setattr__(self, "contradiction_hash", computed)
        else:
            supplied = _hash(
                self.contradiction_hash, "contradiction_hash"
            )
            if supplied != computed:
                raise MetacognitiveContradictionDetectionIntegrityError(
                    "contradiction_hash does not match contradiction content"
                )
            object.__setattr__(self, "contradiction_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scope": self.scope,
            "subject": self.subject,
            "predicate": self.predicate,
            "values_json": list(self.values_json),
            "source_event_sequences": list(self.source_event_sequences),
            "source_event_hashes": list(self.source_event_hashes),
            "assertion_hashes": list(self.assertion_hashes),
            "affected_step_ids": list(self.affected_step_ids),
            "risk_level": self.risk_level.value,
            "confidence_bp": self.confidence_bp,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_contradiction",
            "version": self.version,
            "contradiction_id": self.contradiction_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.contradiction_hash != self.compute_hash():
            raise MetacognitiveContradictionDetectionIntegrityError(
                "contradiction_hash does not match contradiction content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["contradiction_hash"] = self.contradiction_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        kind: MetacognitiveContradictionKind | str,
        scope: str,
        subject: str,
        predicate: str,
        values_json: Iterable[str],
        source_event_sequences: Iterable[int],
        source_event_hashes: Iterable[str],
        assertion_hashes: Iterable[str] = (),
        affected_step_ids: Iterable[str] = (),
        risk_level: MetacognitiveRiskLevel | str,
        confidence_bp: int,
    ) -> "MetacognitiveContradiction":
        normalized_kind = MetacognitiveContradictionKind(kind)
        normalized_scope = _identifier(scope, "scope", _SCOPE)
        normalized_subject = _identifier(subject, "subject", _SUBJECT)
        normalized_predicate = _identifier(
            predicate, "predicate", _PREDICATE
        )
        canonical_values: list[str] = []
        for raw_value in values_json:
            raw_text = _text(raw_value, "value_json")
            try:
                decoded = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise MetacognitiveContradictionDetectionError(
                    "contradiction value_json is invalid"
                ) from exc
            canonical_values.append(canonical_json(_thaw_json(decoded)))
        normalized_values = tuple(sorted(set(canonical_values)))
        normalized_sequences = _positive_integers(
            source_event_sequences,
            "source_event_sequence",
            required=True,
        )
        normalized_event_hashes = _hashes(
            source_event_hashes,
            "source_event_hash",
            required=True,
        )
        normalized_assertion_hashes = _hashes(
            assertion_hashes, "assertion_hash"
        )
        normalized_steps = _identifiers(
            affected_step_ids, "affected_step_id", _STEP_ID
        )
        normalized_risk = MetacognitiveRiskLevel(risk_level)
        normalized_confidence = _integer(
            confidence_bp,
            "confidence_bp",
            minimum=0,
            maximum=10_000,
        )
        identity = {
            "kind": normalized_kind.value,
            "scope": normalized_scope,
            "subject": normalized_subject,
            "predicate": normalized_predicate,
            "values_json": list(normalized_values),
            "source_event_sequences": list(normalized_sequences),
            "source_event_hashes": list(normalized_event_hashes),
            "assertion_hashes": list(normalized_assertion_hashes),
            "affected_step_ids": list(normalized_steps),
            "risk_level": normalized_risk.value,
            "confidence_bp": normalized_confidence,
        }
        return cls(
            contradiction_id=(
                f"metacognitive-contradiction:{_sha256_document(identity)}"
            ),
            kind=normalized_kind,
            scope=normalized_scope,
            subject=normalized_subject,
            predicate=normalized_predicate,
            values_json=normalized_values,
            source_event_sequences=normalized_sequences,
            source_event_hashes=normalized_event_hashes,
            assertion_hashes=normalized_assertion_hashes,
            affected_step_ids=normalized_steps,
            risk_level=normalized_risk,
            confidence_bp=normalized_confidence,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveContradiction":
        if data.get("record_type") != "metacognitive_contradiction":
            raise MetacognitiveContradictionDetectionError(
                "record_type must be metacognitive_contradiction"
            )
        if "contradiction_hash" not in data:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "serialized contradiction is missing contradiction_hash"
            )
        return cls(
            contradiction_id=data["contradiction_id"],
            kind=data["kind"],
            scope=data["scope"],
            subject=data["subject"],
            predicate=data["predicate"],
            values_json=tuple(data["values_json"]),
            source_event_sequences=tuple(
                data["source_event_sequences"]
            ),
            source_event_hashes=tuple(data["source_event_hashes"]),
            assertion_hashes=tuple(data["assertion_hashes"]),
            affected_step_ids=tuple(data["affected_step_ids"]),
            risk_level=data["risk_level"],
            confidence_bp=data["confidence_bp"],
            contradiction_hash=data["contradiction_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "MetacognitiveContradiction":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "metacognitive contradiction JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveContradictionDetectionError(
                "metacognitive contradiction JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveContradictionDetectionRequest:
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
    version: int = METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _identifier(self.request_id, "request_id")
        )
        policy = MetacognitiveContradictionDetectionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
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
            raise MetacognitiveContradictionDetectionIntegrityError(
                "embedded supervision context is invalid"
            ) from exc
        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != context.context_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
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
        if (
            self.journal_evidence_reference
            not in context.evidence_references
        ):
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal evidence reference is not bound to the context"
            )
        if self.journal_plan_id != context.plan_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal plan does not match the supervision context"
            )
        if self.journal_hash != context.journal_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal hash does not match the supervision context"
            )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < context.observed_at:
            raise MetacognitiveContradictionDetectionPolicyError(
                "contradiction detection cannot precede context observation"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if (
            self.version
            != METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveContradictionDetectionError(
                "unsupported contradiction-detection request format version"
            )
        expected_id = (
            f"metacognitive-contradiction-request:"
            f"{self.compute_identity_hash()}"
        )
        if self.request_id != expected_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveContradictionDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveContradictionDetectionPolicy:
        return MetacognitiveContradictionDetectionPolicy.from_json(
            self.policy_json
        )

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
            "journal_evidence_reference": (
                self.journal_evidence_reference
            ),
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_contradiction_detection_request",
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
            raise MetacognitiveContradictionDetectionIntegrityError(
                "request_hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        policy: MetacognitiveContradictionDetectionPolicy,
        context: MetacognitiveSupervisionContext,
        journal: ExecutionJournal,
        journal_evidence_reference: str,
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveContradictionDetectionRequest":
        if not isinstance(
            policy, MetacognitiveContradictionDetectionPolicy
        ):
            raise MetacognitiveContradictionDetectionError(
                "policy must be a "
                "MetacognitiveContradictionDetectionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveContradictionDetectionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveContradictionDetectionError(
                "journal must be an ExecutionJournal"
            )
        context.verify_hash()
        seal = journal.validate()
        reference = _identifier(
            journal_evidence_reference,
            "journal_evidence_reference",
        )
        normalized_requested_by = _identifier(
            requested_by, "requested_by", _AGENT_ID
        )
        normalized_requested_at = _utc_timestamp(
            requested_at, "requested_at"
        )
        normalized_reason = _text(reason, "reason")
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "journal_plan_id": seal.plan_id,
            "journal_event_count": seal.event_count,
            "journal_head_hash": seal.head_hash,
            "journal_hash": seal.journal_hash,
            "journal_evidence_reference": reference,
            "requested_by": normalized_requested_by,
            "requested_at": normalized_requested_at,
            "reason": normalized_reason,
        }
        return cls(
            request_id=(
                "metacognitive-contradiction-request:"
                f"{_sha256_document(identity)}"
            ),
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            journal_plan_id=seal.plan_id,
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
            journal_evidence_reference=reference,
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveContradictionDetectionRequest":
        if (
            data.get("record_type")
            != "metacognitive_contradiction_detection_request"
        ):
            raise MetacognitiveContradictionDetectionError(
                "record_type must be "
                "metacognitive_contradiction_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveContradictionDetectionIntegrityError(
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
            journal_evidence_reference=data[
                "journal_evidence_reference"
            ],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            reason=data["reason"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveContradictionDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveContradictionDetectionResult:
    result_id: str
    request_json: str
    request_hash: str
    context_hash: str
    journal_hash: str
    status: MetacognitiveContradictionDetectionStatus
    assertions_json: tuple[str, ...]
    assertion_hashes: tuple[str, ...]
    contradictions_json: tuple[str, ...]
    contradiction_hashes: tuple[str, ...]
    findings_json: tuple[str, ...]
    finding_hashes: tuple[str, ...]
    analyzed_by: str
    analyzed_at: str
    result_hash: str | None = None
    version: int = METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "result_id", _identifier(self.result_id, "result_id")
        )
        request = MetacognitiveContradictionDetectionRequest.from_json(
            _text(self.request_json, "request_json")
        )
        request.verify_hash()
        supplied_request_hash = _hash(self.request_hash, "request_hash")
        if supplied_request_hash != request.request_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "request_hash does not match embedded request"
            )
        object.__setattr__(self, "request_json", request.to_json())
        object.__setattr__(self, "request_hash", supplied_request_hash)
        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != request.context_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "result context_hash does not match request"
            )
        object.__setattr__(self, "context_hash", supplied_context_hash)
        supplied_journal_hash = _hash(self.journal_hash, "journal_hash")
        if supplied_journal_hash != request.journal_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "result journal_hash does not match request"
            )
        object.__setattr__(self, "journal_hash", supplied_journal_hash)
        try:
            status = MetacognitiveContradictionDetectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        assertions = tuple(
            sorted(
                (
                    MetacognitiveAssertion.from_json(
                        _text(payload, "assertion_json")
                    )
                    for payload in self.assertions_json
                ),
                key=lambda item: item.assertion_id,
            )
        )
        for assertion in assertions:
            assertion.verify_hash()
        hashes = tuple(item.assertion_hash or "" for item in assertions)
        supplied_hashes = _hashes(
            self.assertion_hashes, "assertion_hash"
        )
        if tuple(sorted(hashes)) != supplied_hashes:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "assertion_hashes do not match embedded assertions"
            )
        object.__setattr__(
            self,
            "assertions_json",
            tuple(item.to_json() for item in assertions),
        )
        object.__setattr__(
            self, "assertion_hashes", tuple(sorted(hashes))
        )

        contradictions = tuple(
            sorted(
                (
                    MetacognitiveContradiction.from_json(
                        _text(payload, "contradiction_json")
                    )
                    for payload in self.contradictions_json
                ),
                key=lambda item: item.contradiction_id,
            )
        )
        for contradiction in contradictions:
            contradiction.verify_hash()
        contradiction_hashes = tuple(
            item.contradiction_hash or "" for item in contradictions
        )
        supplied_contradiction_hashes = _hashes(
            self.contradiction_hashes, "contradiction_hash"
        )
        if (
            tuple(sorted(contradiction_hashes))
            != supplied_contradiction_hashes
        ):
            raise MetacognitiveContradictionDetectionIntegrityError(
                "contradiction_hashes do not match embedded contradictions"
            )
        object.__setattr__(
            self,
            "contradictions_json",
            tuple(item.to_json() for item in contradictions),
        )
        object.__setattr__(
            self,
            "contradiction_hashes",
            tuple(sorted(contradiction_hashes)),
        )

        findings = tuple(
            sorted(
                (
                    MetacognitiveSupervisionFinding.from_json(
                        _text(payload, "finding_json")
                    )
                    for payload in self.findings_json
                ),
                key=lambda item: item.finding_id,
            )
        )
        for finding in findings:
            finding.verify_hash()
            if (
                finding.context_hash != request.context_hash
                or finding.kind is not MetacognitiveFindingKind.CONTRADICTION
            ):
                raise MetacognitiveContradictionDetectionIntegrityError(
                    "finding is not a contradiction bound to the request "
                    "context"
                )
        finding_hashes = tuple(
            item.finding_hash or "" for item in findings
        )
        supplied_finding_hashes = _hashes(
            self.finding_hashes, "finding_hash"
        )
        if tuple(sorted(finding_hashes)) != supplied_finding_hashes:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "finding_hashes do not match embedded findings"
            )
        object.__setattr__(
            self,
            "findings_json",
            tuple(item.to_json() for item in findings),
        )
        object.__setattr__(
            self, "finding_hashes", tuple(sorted(finding_hashes))
        )

        has_contradictions = bool(contradictions)
        if (
            status
            is MetacognitiveContradictionDetectionStatus.CONTRADICTIONS_DETECTED
        ) != has_contradictions:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "result status does not match contradiction content"
            )
        if len(findings) != len(contradictions):
            raise MetacognitiveContradictionDetectionIntegrityError(
                "each contradiction must have exactly one finding"
            )
        object.__setattr__(
            self,
            "analyzed_by",
            _identifier(self.analyzed_by, "analyzed_by", _AGENT_ID),
        )
        analyzed_at = _utc_timestamp(self.analyzed_at, "analyzed_at")
        if analyzed_at != request.requested_at:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "deterministic analysis timestamp must equal requested_at"
            )
        object.__setattr__(self, "analyzed_at", analyzed_at)
        if (
            self.version
            != METACOGNITIVE_CONTRADICTION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveContradictionDetectionError(
                "unsupported contradiction-detection result format version"
            )
        expected_id = (
            f"metacognitive-contradiction-result:"
            f"{self.compute_identity_hash()}"
        )
        if self.result_id != expected_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "result_id does not match result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveContradictionDetectionIntegrityError(
                    "result_hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def request(self) -> MetacognitiveContradictionDetectionRequest:
        return MetacognitiveContradictionDetectionRequest.from_json(
            self.request_json
        )

    @property
    def assertions(self) -> tuple[MetacognitiveAssertion, ...]:
        return tuple(
            MetacognitiveAssertion.from_json(payload)
            for payload in self.assertions_json
        )

    @property
    def contradictions(self) -> tuple[MetacognitiveContradiction, ...]:
        return tuple(
            MetacognitiveContradiction.from_json(payload)
            for payload in self.contradictions_json
        )

    @property
    def findings(self) -> tuple[MetacognitiveSupervisionFinding, ...]:
        return tuple(
            MetacognitiveSupervisionFinding.from_json(payload)
            for payload in self.findings_json
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "context_hash": self.context_hash,
            "journal_hash": self.journal_hash,
            "status": self.status.value,
            "assertion_hashes": list(self.assertion_hashes),
            "contradiction_hashes": list(self.contradiction_hashes),
            "finding_hashes": list(self.finding_hashes),
            "analyzed_by": self.analyzed_by,
            "analyzed_at": self.analyzed_at,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_contradiction_detection_result",
            "version": self.version,
            "result_id": self.result_id,
            "request_json": self.request_json,
            "assertions_json": list(self.assertions_json),
            "contradictions_json": list(self.contradictions_json),
            "findings_json": list(self.findings_json),
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise MetacognitiveContradictionDetectionIntegrityError(
                "result_hash does not match result content"
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
        request: MetacognitiveContradictionDetectionRequest,
        assertions: Sequence[MetacognitiveAssertion],
        contradictions: Sequence[MetacognitiveContradiction],
        findings: Sequence[MetacognitiveSupervisionFinding],
        analyzed_by: str,
    ) -> "MetacognitiveContradictionDetectionResult":
        if not isinstance(
            request, MetacognitiveContradictionDetectionRequest
        ):
            raise MetacognitiveContradictionDetectionError(
                "request must be a "
                "MetacognitiveContradictionDetectionRequest"
            )
        request.verify_hash()
        ordered_assertions = tuple(
            sorted(assertions, key=lambda item: item.assertion_id)
        )
        ordered_contradictions = tuple(
            sorted(contradictions, key=lambda item: item.contradiction_id)
        )
        ordered_findings = tuple(
            sorted(findings, key=lambda item: item.finding_id)
        )
        status = (
            MetacognitiveContradictionDetectionStatus.CONTRADICTIONS_DETECTED
            if ordered_contradictions
            else MetacognitiveContradictionDetectionStatus.CLEAR
        )
        normalized_analyzed_by = _identifier(
            analyzed_by, "analyzed_by", _AGENT_ID
        )
        assertion_hashes = tuple(
            sorted(item.assertion_hash or "" for item in ordered_assertions)
        )
        contradiction_hashes = tuple(
            sorted(
                item.contradiction_hash or ""
                for item in ordered_contradictions
            )
        )
        finding_hashes = tuple(
            sorted(item.finding_hash or "" for item in ordered_findings)
        )
        identity = {
            "request_hash": request.request_hash,
            "context_hash": request.context_hash,
            "journal_hash": request.journal_hash,
            "status": status.value,
            "assertion_hashes": list(assertion_hashes),
            "contradiction_hashes": list(contradiction_hashes),
            "finding_hashes": list(finding_hashes),
            "analyzed_by": normalized_analyzed_by,
            "analyzed_at": request.requested_at,
        }
        return cls(
            result_id=(
                "metacognitive-contradiction-result:"
                f"{_sha256_document(identity)}"
            ),
            request_json=request.to_json(),
            request_hash=request.request_hash or "",
            context_hash=request.context_hash,
            journal_hash=request.journal_hash,
            status=status,
            assertions_json=tuple(
                item.to_json() for item in ordered_assertions
            ),
            assertion_hashes=assertion_hashes,
            contradictions_json=tuple(
                item.to_json() for item in ordered_contradictions
            ),
            contradiction_hashes=contradiction_hashes,
            findings_json=tuple(
                item.to_json() for item in ordered_findings
            ),
            finding_hashes=finding_hashes,
            analyzed_by=normalized_analyzed_by,
            analyzed_at=request.requested_at,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveContradictionDetectionResult":
        if (
            data.get("record_type")
            != "metacognitive_contradiction_detection_result"
        ):
            raise MetacognitiveContradictionDetectionError(
                "record_type must be "
                "metacognitive_contradiction_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            result_id=data["result_id"],
            request_json=data["request_json"],
            request_hash=data["request_hash"],
            context_hash=data["context_hash"],
            journal_hash=data["journal_hash"],
            status=data["status"],
            assertions_json=tuple(data["assertions_json"]),
            assertion_hashes=tuple(data["assertion_hashes"]),
            contradictions_json=tuple(data["contradictions_json"]),
            contradiction_hashes=tuple(data["contradiction_hashes"]),
            findings_json=tuple(data["findings_json"]),
            finding_hashes=tuple(data["finding_hashes"]),
            analyzed_by=data["analyzed_by"],
            analyzed_at=data["analyzed_at"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls, payload: str
    ) -> "MetacognitiveContradictionDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveContradictionDetectionError(
                "contradiction-detection result JSON must be an object"
            )
        return cls.from_dict(data)


def _assertions_from_event(
    event: ExecutionEvent,
    policy: MetacognitiveContradictionDetectionPolicy,
) -> tuple[MetacognitiveAssertion, ...]:
    raw = event.payload.get(policy.assertion_payload_key)
    if raw is None:
        return ()
    if not isinstance(raw, (tuple, list)):
        raise MetacognitiveContradictionDetectionIntegrityError(
            "metacognitive assertion envelope must be a JSON array"
        )
    if len(raw) > policy.maximum_assertions_per_event:
        raise MetacognitiveContradictionDetectionPolicyError(
            "event exceeds maximum_assertions_per_event"
        )
    assertions: list[MetacognitiveAssertion] = []
    allowed_keys = frozenset(
        {"scope", "subject", "predicate", "value", "step_id"}
    )
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise MetacognitiveContradictionDetectionIntegrityError(
                f"assertion {index} must be a JSON object"
            )
        unknown = set(item) - allowed_keys
        if unknown:
            raise MetacognitiveContradictionDetectionIntegrityError(
                f"assertion {index} contains unsupported fields"
            )
        missing = {"scope", "subject", "predicate", "value"} - set(item)
        if missing:
            raise MetacognitiveContradictionDetectionIntegrityError(
                f"assertion {index} is missing required fields"
            )
        assertions.append(
            MetacognitiveAssertion.capture(
                scope=item["scope"],
                subject=item["subject"],
                predicate=item["predicate"],
                value=item["value"],
                event=event,
                step_id=item.get("step_id"),
            )
        )
    return tuple(assertions)


def _risk_for_assertion_conflict(
    distinct_values: int,
    policy: MetacognitiveContradictionDetectionPolicy,
) -> MetacognitiveRiskLevel:
    if distinct_values >= policy.critical_risk_distinct_values:
        return MetacognitiveRiskLevel.CRITICAL
    if distinct_values >= policy.high_risk_distinct_values:
        return MetacognitiveRiskLevel.HIGH
    return MetacognitiveRiskLevel.MEDIUM


def _confidence_for_conflict(
    *,
    source_events: int,
    distinct_values: int,
    policy: MetacognitiveContradictionDetectionPolicy,
) -> int:
    return min(
        10_000,
        policy.base_confidence_bp
        + max(0, source_events - 1)
        * policy.source_confidence_increment_bp
        + max(0, distinct_values - 2)
        * policy.value_confidence_increment_bp,
    )


def _detect_assertion_conflicts(
    assertions: Sequence[MetacognitiveAssertion],
    policy: MetacognitiveContradictionDetectionPolicy,
) -> tuple[MetacognitiveContradiction, ...]:
    grouped: dict[
        tuple[str, str, str], list[MetacognitiveAssertion]
    ] = {}
    for assertion in assertions:
        grouped.setdefault(
            (assertion.scope, assertion.subject, assertion.predicate),
            [],
        ).append(assertion)
    contradictions: list[MetacognitiveContradiction] = []
    for (scope, subject, predicate), group in sorted(grouped.items()):
        values = tuple(sorted({item.value_json for item in group}))
        if len(values) < 2:
            continue
        sequences = tuple(
            sorted({item.source_event_sequence for item in group})
        )
        hashes = tuple(
            sorted({item.source_event_hash for item in group})
        )
        assertion_hashes = tuple(
            sorted({item.assertion_hash or "" for item in group})
        )
        steps = tuple(
            sorted({item.step_id for item in group if item.step_id})
        )
        risk = _risk_for_assertion_conflict(len(values), policy)
        confidence = _confidence_for_conflict(
            source_events=len(sequences),
            distinct_values=len(values),
            policy=policy,
        )
        contradictions.append(
            MetacognitiveContradiction.create(
                kind=MetacognitiveContradictionKind.ASSERTION_CONFLICT,
                scope=scope,
                subject=subject,
                predicate=predicate,
                values_json=values,
                source_event_sequences=sequences,
                source_event_hashes=hashes,
                assertion_hashes=assertion_hashes,
                affected_step_ids=steps,
                risk_level=risk,
                confidence_bp=confidence,
            )
        )
    return tuple(contradictions)


def _detect_completed_step_regressions(
    events: Sequence[ExecutionEvent],
    context: MetacognitiveSupervisionContext,
    policy: MetacognitiveContradictionDetectionPolicy,
) -> tuple[MetacognitiveContradiction, ...]:
    by_step: dict[str, list[ExecutionEvent]] = {}
    for event in events:
        if event.step_id is not None:
            by_step.setdefault(event.step_id, []).append(event)
    contradictions: list[MetacognitiveContradiction] = []
    for step_id, step_events in sorted(by_step.items()):
        completed = [
            event
            for event in step_events
            if event.event_type is ExecutionEventType.STEP_COMPLETED
        ]
        if not completed:
            continue
        first_completion = min(completed, key=lambda item: item.sequence)
        regressions = [
            event
            for event in step_events
            if event.sequence > first_completion.sequence
            and event.event_type
            in {
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.STEP_FAILED,
            }
        ]
        if not regressions:
            continue
        for regression in regressions:
            state = (
                "blocked"
                if regression.event_type is ExecutionEventType.STEP_BLOCKED
                else "failed"
            )
            confidence = _confidence_for_conflict(
                source_events=2,
                distinct_values=2,
                policy=policy,
            )
            contradictions.append(
                MetacognitiveContradiction.create(
                    kind=(
                        MetacognitiveContradictionKind
                        .COMPLETED_STEP_REGRESSION
                    ),
                    scope=f"checkpoint:{context.checkpoint_hash}",
                    subject=f"step:{step_id}",
                    predicate="lifecycle-state",
                    values_json=(
                        canonical_json("completed"),
                        canonical_json(state),
                    ),
                    source_event_sequences=(
                        first_completion.sequence,
                        regression.sequence,
                    ),
                    source_event_hashes=(
                        first_completion.event_hash or "",
                        regression.event_hash or "",
                    ),
                    affected_step_ids=(step_id,),
                    risk_level=MetacognitiveRiskLevel.HIGH,
                    confidence_bp=confidence,
                )
            )
    return tuple(contradictions)


def _finding_for_contradiction(
    *,
    contradiction: MetacognitiveContradiction,
    request: MetacognitiveContradictionDetectionRequest,
    context: MetacognitiveSupervisionContext,
) -> MetacognitiveSupervisionFinding:
    if (
        contradiction.kind
        is MetacognitiveContradictionKind.ASSERTION_CONFLICT
    ):
        summary = (
            "Contradictory assertions detected for "
            f"{contradiction.subject}.{contradiction.predicate} "
            f"within scope {contradiction.scope}: "
            f"{len(contradiction.values_json)} distinct values across "
            f"{len(contradiction.source_event_sequences)} source events."
        )
    else:
        summary = (
            "Completed step regressed to an incompatible state for "
            f"{contradiction.subject} within scope "
            f"{contradiction.scope}."
        )
    return MetacognitiveSupervisionFinding.from_context(
        context=context,
        kind=MetacognitiveFindingKind.CONTRADICTION,
        risk_level=contradiction.risk_level,
        summary=summary,
        evidence_references=(request.journal_evidence_reference,),
        affected_step_ids=contradiction.affected_step_ids,
        confidence_bp=contradiction.confidence_bp,
    )


class MetacognitiveContradictionDetector:
    """Read-only deterministic contradiction detector."""

    def detect(
        self,
        *,
        request: MetacognitiveContradictionDetectionRequest,
        journal: ExecutionJournal,
        analyzed_by: str,
    ) -> MetacognitiveContradictionDetectionResult:
        if not isinstance(
            request, MetacognitiveContradictionDetectionRequest
        ):
            raise MetacognitiveContradictionDetectionError(
                "request must be a "
                "MetacognitiveContradictionDetectionRequest"
            )
        if not isinstance(journal, ExecutionJournal):
            raise MetacognitiveContradictionDetectionError(
                "journal must be an ExecutionJournal"
            )
        request.verify_hash()
        context = request.context
        context.verify_hash()
        before_context = context.to_json()
        try:
            before_journal = journal.to_jsonl()
            seal = journal.validate()
        except ExecutionJournalError as exc:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "execution journal is invalid"
            ) from exc
        if seal.plan_id != request.journal_plan_id:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal plan does not match request"
            )
        if seal.event_count != request.journal_event_count:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal event count does not match request"
            )
        if seal.head_hash != request.journal_head_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal head hash does not match request"
            )
        if seal.journal_hash != request.journal_hash:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "journal hash does not match request"
            )

        policy = request.policy
        events = journal.replay()
        assertions: tuple[MetacognitiveAssertion, ...] = ()
        if policy.include_assertion_conflicts:
            extracted = tuple(
                assertion
                for event in events
                for assertion in _assertions_from_event(event, policy)
            )
            assertions = tuple(
                sorted(
                    {
                        assertion.assertion_id: assertion
                        for assertion in extracted
                    }.values(),
                    key=lambda item: item.assertion_id,
                )
            )
        contradictions: list[MetacognitiveContradiction] = []
        if policy.include_assertion_conflicts:
            contradictions.extend(
                _detect_assertion_conflicts(assertions, policy)
            )
        if policy.include_completed_step_regressions:
            contradictions.extend(
                _detect_completed_step_regressions(
                    events, context, policy
                )
            )
        ordered = tuple(
            sorted(
                {
                    contradiction.contradiction_id: contradiction
                    for contradiction in contradictions
                }.values(),
                key=lambda item: item.contradiction_id,
            )
        )
        findings = tuple(
            _finding_for_contradiction(
                contradiction=contradiction,
                request=request,
                context=context,
            )
            for contradiction in ordered
        )
        result = MetacognitiveContradictionDetectionResult.create(
            request=request,
            assertions=assertions,
            contradictions=ordered,
            findings=findings,
            analyzed_by=analyzed_by,
        )
        result.verify_hash()

        if context.to_json() != before_context:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "supervision context changed during analysis"
            )
        if journal.to_jsonl() != before_journal:
            raise MetacognitiveContradictionDetectionIntegrityError(
                "execution journal changed during analysis"
            )
        return result
