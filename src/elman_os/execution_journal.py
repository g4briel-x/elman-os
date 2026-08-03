"""Append-only, hash-chained execution journal for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .agent_contracts import FrozenJson, canonical_json


GENESIS_HASH = "0" * 64
JOURNAL_FORMAT_VERSION = 1

_PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionJournalError(ValueError):
    """A journal record or operation violates the strict contract."""


class JournalIntegrityError(ExecutionJournalError):
    """The event chain or journal seal is inconsistent."""


class JournalSequenceError(ExecutionJournalError):
    """An event sequence is not strictly monotonic."""


class JournalTimestampError(ExecutionJournalError):
    """A timestamp is invalid or moves backwards."""


class ExecutionEventType(StrEnum):
    PLAN_CREATED = "plan.created"
    PLAN_APPROVED = "plan.approved"
    PLAN_STARTED = "plan.started"
    PLAN_BLOCKED = "plan.blocked"
    PLAN_FAILED = "plan.failed"
    PLAN_COMPLETED = "plan.completed"
    STEP_READY = "step.ready"
    STEP_ASSIGNED = "step.assigned"
    STEP_APPROVED = "step.approved"
    STEP_STARTED = "step.started"
    STEP_BLOCKED = "step.blocked"
    STEP_FAILED = "step.failed"
    STEP_COMPLETED = "step.completed"


_STEP_EVENT_TYPES = frozenset(
    {
        ExecutionEventType.STEP_READY,
        ExecutionEventType.STEP_ASSIGNED,
        ExecutionEventType.STEP_APPROVED,
        ExecutionEventType.STEP_STARTED,
        ExecutionEventType.STEP_BLOCKED,
        ExecutionEventType.STEP_FAILED,
        ExecutionEventType.STEP_COMPLETED,
    }
)

_PLAN_EVENT_TYPES = frozenset(set(ExecutionEventType) - _STEP_EVENT_TYPES)

_AGENT_REQUIRED_TYPES = frozenset(
    {
        ExecutionEventType.STEP_ASSIGNED,
        ExecutionEventType.STEP_STARTED,
        ExecutionEventType.STEP_BLOCKED,
        ExecutionEventType.STEP_FAILED,
        ExecutionEventType.STEP_COMPLETED,
    }
)

_TERMINAL_PLAN_TYPES = frozenset(
    {
        ExecutionEventType.PLAN_FAILED,
        ExecutionEventType.PLAN_COMPLETED,
    }
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionJournalError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str],
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ExecutionJournalError(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ExecutionJournalError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _freeze_json(value: Any, path: str = "value") -> FrozenJson:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutionJournalError(
                f"{path} contains a non-finite number"
            )
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExecutionJournalError(
                f"{path} contains a non-string key"
            )
        frozen: dict[str, FrozenJson] = {}
        for raw_key in sorted(value):
            key = _text(raw_key, f"{path} key")
            frozen[key] = _freeze_json(
                value[raw_key],
                f"{path}.{key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise ExecutionJournalError(
        f"{path} contains non-JSON type {type(value).__name__}"
    )


def _thaw_json(value: FrozenJson) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _utc_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise JournalTimestampError(
                "timestamp datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise JournalTimestampError(
                "timestamp datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise JournalTimestampError(
                "timestamp must be an ISO-8601 UTC value ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise JournalTimestampError(
                "timestamp is not valid ISO-8601 UTC"
            ) from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise JournalTimestampError(
                "timestamp must be UTC"
            )
    else:
        raise JournalTimestampError(
            "timestamp must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _sha256_document(data: Mapping[str, Any]) -> str:
    encoded = canonical_json(data).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    sequence: int
    event_type: ExecutionEventType
    timestamp: str
    plan_id: str
    previous_hash: str
    step_id: str | None = None
    agent_id: str | None = None
    payload: Mapping[str, FrozenJson] = field(default_factory=dict)
    event_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise JournalSequenceError(
                "sequence must be a positive integer"
            )

        try:
            event_type = ExecutionEventType(self.event_type)
        except (TypeError, ValueError) as exc:
            raise ExecutionJournalError(
                "event_type is invalid"
            ) from exc
        object.__setattr__(self, "event_type", event_type)

        object.__setattr__(
            self,
            "timestamp",
            _utc_timestamp(self.timestamp),
        )
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id", _PLAN_ID),
        )
        object.__setattr__(
            self,
            "previous_hash",
            _hash(self.previous_hash, "previous_hash"),
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

        if event_type in _STEP_EVENT_TYPES and self.step_id is None:
            raise ExecutionJournalError(
                "step event requires step_id"
            )
        if event_type in _PLAN_EVENT_TYPES and self.step_id is not None:
            raise ExecutionJournalError(
                "plan event cannot reference step_id"
            )
        if event_type in _AGENT_REQUIRED_TYPES and self.agent_id is None:
            raise ExecutionJournalError(
                f"{event_type.value} requires agent_id"
            )

        frozen = _freeze_json(dict(self.payload), "payload")
        if not isinstance(frozen, Mapping):
            raise ExecutionJournalError(
                "payload must be a JSON object"
            )
        object.__setattr__(self, "payload", frozen)

        computed = self.compute_hash()
        if self.event_hash is None:
            object.__setattr__(self, "event_hash", computed)
        else:
            supplied = _hash(self.event_hash, "event_hash")
            if supplied != computed:
                raise JournalIntegrityError(
                    f"event hash mismatch at sequence {self.sequence}"
                )
            object.__setattr__(self, "event_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "event",
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "payload": _thaw_json(self.payload),
            "previous_hash": self.previous_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.event_hash != self.compute_hash():
            raise JournalIntegrityError(
                f"event hash mismatch at sequence {self.sequence}"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["event_hash"] = self.event_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ExecutionEvent":
        if data.get("record_type") != "event":
            raise ExecutionJournalError(
                "record_type must be event"
            )
        if "event_hash" not in data:
            raise JournalIntegrityError(
                "serialized event is missing event_hash"
            )
        return cls(
            sequence=data["sequence"],
            event_type=ExecutionEventType(data["event_type"]),
            timestamp=data["timestamp"],
            plan_id=data["plan_id"],
            step_id=data.get("step_id"),
            agent_id=data.get("agent_id"),
            payload=data.get("payload", {}),
            previous_hash=data["previous_hash"],
            event_hash=data["event_hash"],
        )

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionEvent":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExecutionJournalError(
                "event JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ExecutionJournalError(
                "event JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class JournalSeal:
    plan_id: str
    event_count: int
    head_hash: str
    journal_hash: str
    algorithm: str = "sha256"
    version: int = JOURNAL_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id", _PLAN_ID),
        )
        if (
            isinstance(self.event_count, bool)
            or not isinstance(self.event_count, int)
            or self.event_count < 0
        ):
            raise ExecutionJournalError(
                "event_count must be a non-negative integer"
            )
        object.__setattr__(
            self,
            "head_hash",
            _hash(self.head_hash, "head_hash"),
        )
        object.__setattr__(
            self,
            "journal_hash",
            _hash(self.journal_hash, "journal_hash"),
        )
        if self.algorithm != "sha256":
            raise ExecutionJournalError(
                "journal seal algorithm must be sha256"
            )
        if self.version != JOURNAL_FORMAT_VERSION:
            raise ExecutionJournalError(
                "unsupported journal format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "seal",
            "version": self.version,
            "algorithm": self.algorithm,
            "plan_id": self.plan_id,
            "event_count": self.event_count,
            "head_hash": self.head_hash,
            "journal_hash": self.journal_hash,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "JournalSeal":
        if data.get("record_type") != "seal":
            raise ExecutionJournalError(
                "record_type must be seal"
            )
        return cls(
            plan_id=data["plan_id"],
            event_count=data["event_count"],
            head_hash=data["head_hash"],
            journal_hash=data["journal_hash"],
            algorithm=data.get("algorithm", ""),
            version=data.get("version", 0),
        )


class ExecutionJournal:
    """In-memory append-only journal with a SHA-256 integrity chain."""

    def __init__(self, plan_id: str) -> None:
        self._plan_id = _identifier(
            plan_id,
            "plan_id",
            _PLAN_ID,
        )
        self._events: list[ExecutionEvent] = []

    @property
    def plan_id(self) -> str:
        return self._plan_id

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def head_hash(self) -> str:
        if not self._events:
            return GENESIS_HASH
        event_hash = self._events[-1].event_hash
        assert event_hash is not None
        return event_hash

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[ExecutionEvent]:
        return iter(self.events)

    def append(
        self,
        event_type: ExecutionEventType,
        timestamp: str | datetime,
        *,
        step_id: str | None = None,
        agent_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ExecutionEvent:
        event = ExecutionEvent(
            sequence=len(self._events) + 1,
            event_type=event_type,
            timestamp=timestamp,
            plan_id=self._plan_id,
            step_id=step_id,
            agent_id=agent_id,
            payload=payload or {},
            previous_hash=self.head_hash,
        )
        self.append_event(event)
        return event

    def append_event(self, event: ExecutionEvent) -> None:
        if not isinstance(event, ExecutionEvent):
            raise ExecutionJournalError(
                "event must be an ExecutionEvent"
            )
        event.verify_hash()

        if event.plan_id != self._plan_id:
            raise ExecutionJournalError(
                "event plan_id does not match journal plan_id"
            )

        expected_sequence = len(self._events) + 1
        if event.sequence != expected_sequence:
            raise JournalSequenceError(
                f"expected sequence {expected_sequence}, "
                f"received {event.sequence}"
            )

        if event.previous_hash != self.head_hash:
            raise JournalIntegrityError(
                f"previous hash mismatch at sequence {event.sequence}"
            )

        if not self._events:
            if event.event_type is not ExecutionEventType.PLAN_CREATED:
                raise ExecutionJournalError(
                    "first event must be plan.created"
                )
        else:
            previous = self._events[-1]
            if previous.event_type in _TERMINAL_PLAN_TYPES:
                raise ExecutionJournalError(
                    "cannot append after terminal plan event"
                )
            if _timestamp_value(event.timestamp) < _timestamp_value(
                previous.timestamp
            ):
                raise JournalTimestampError(
                    "event timestamps must be monotonic"
                )
            if event.event_type is ExecutionEventType.PLAN_CREATED:
                raise ExecutionJournalError(
                    "plan.created can only be the first event"
                )

        self._events.append(event)

    def events_for_step(
        self,
        step_id: str,
    ) -> tuple[ExecutionEvent, ...]:
        normalized = _identifier(
            step_id,
            "step_id",
            _STEP_ID,
        )
        return tuple(
            event
            for event in self._events
            if event.step_id == normalized
        )

    def replay(self) -> tuple[ExecutionEvent, ...]:
        self.validate()
        return self.events

    def _journal_hash(self) -> str:
        return _sha256_document(
            {
                "record_type": "journal_integrity",
                "version": JOURNAL_FORMAT_VERSION,
                "algorithm": "sha256",
                "plan_id": self._plan_id,
                "event_count": len(self._events),
                "head_hash": self.head_hash,
                "event_hashes": [
                    event.event_hash
                    for event in self._events
                ],
            }
        )

    def seal(self) -> JournalSeal:
        self.validate()
        return JournalSeal(
            plan_id=self._plan_id,
            event_count=len(self._events),
            head_hash=self.head_hash,
            journal_hash=self._journal_hash(),
        )

    def validate(self) -> JournalSeal:
        expected_previous = GENESIS_HASH
        previous_timestamp: datetime | None = None

        for index, event in enumerate(self._events, start=1):
            event.verify_hash()
            if event.sequence != index:
                raise JournalSequenceError(
                    f"expected sequence {index}, "
                    f"received {event.sequence}"
                )
            if event.plan_id != self._plan_id:
                raise JournalIntegrityError(
                    f"plan_id mismatch at sequence {event.sequence}"
                )
            if event.previous_hash != expected_previous:
                raise JournalIntegrityError(
                    f"chain mismatch at sequence {event.sequence}"
                )
            if index == 1 and (
                event.event_type
                is not ExecutionEventType.PLAN_CREATED
            ):
                raise JournalIntegrityError(
                    "first event is not plan.created"
                )
            if index > 1 and (
                event.event_type
                is ExecutionEventType.PLAN_CREATED
            ):
                raise JournalIntegrityError(
                    "duplicate plan.created event"
                )
            if index > 1 and (
                self._events[index - 2].event_type
                in _TERMINAL_PLAN_TYPES
            ):
                raise JournalIntegrityError(
                    "event exists after terminal plan event"
                )

            timestamp = _timestamp_value(event.timestamp)
            if (
                previous_timestamp is not None
                and timestamp < previous_timestamp
            ):
                raise JournalTimestampError(
                    "event timestamps are not monotonic"
                )
            previous_timestamp = timestamp

            event_hash = event.event_hash
            assert event_hash is not None
            expected_previous = event_hash

        return JournalSeal(
            plan_id=self._plan_id,
            event_count=len(self._events),
            head_hash=self.head_hash,
            journal_hash=self._journal_hash(),
        )

    def to_jsonl(self) -> str:
        seal = self.seal()
        lines = [
            event.to_json()
            for event in self._events
        ]
        lines.append(seal.to_json())
        return "\n".join(lines) + "\n"

    @classmethod
    def from_jsonl(
        cls,
        payload: str,
        *,
        expected_plan_id: str | None = None,
        expected_event_count: int | None = None,
        expected_head_hash: str | None = None,
        expected_journal_hash: str | None = None,
    ) -> "ExecutionJournal":
        if not isinstance(payload, str) or not payload:
            raise ExecutionJournalError(
                "journal JSONL must be a non-empty string"
            )

        text = payload.removeprefix("\ufeff")
        lines = text.splitlines()
        if not lines or any(not line.strip() for line in lines):
            raise ExecutionJournalError(
                "journal JSONL cannot contain blank records"
            )

        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExecutionJournalError(
                    f"invalid JSON at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ExecutionJournalError(
                    f"line {line_number} must contain a JSON object"
                )
            records.append(record)

        seal_record = records[-1]
        if seal_record.get("record_type") != "seal":
            raise JournalIntegrityError(
                "journal is missing its final seal"
            )
        if any(
            record.get("record_type") != "event"
            for record in records[:-1]
        ):
            raise ExecutionJournalError(
                "all records before the seal must be events"
            )

        seal = JournalSeal.from_dict(seal_record)
        journal = cls(seal.plan_id)

        for record in records[:-1]:
            journal.append_event(
                ExecutionEvent.from_dict(record)
            )

        computed = journal.validate()

        if seal.event_count != computed.event_count:
            raise JournalIntegrityError(
                "journal event count does not match seal"
            )
        if seal.head_hash != computed.head_hash:
            raise JournalIntegrityError(
                "journal head hash does not match seal"
            )
        if seal.journal_hash != computed.journal_hash:
            raise JournalIntegrityError(
                "journal hash does not match seal"
            )

        if expected_plan_id is not None:
            normalized_plan_id = _identifier(
                expected_plan_id,
                "expected_plan_id",
                _PLAN_ID,
            )
            if normalized_plan_id != journal.plan_id:
                raise JournalIntegrityError(
                    "journal plan_id does not match expected_plan_id"
                )

        if expected_event_count is not None:
            if (
                isinstance(expected_event_count, bool)
                or not isinstance(expected_event_count, int)
                or expected_event_count < 0
            ):
                raise ExecutionJournalError(
                    "expected_event_count must be non-negative"
                )
            if expected_event_count != journal.event_count:
                raise JournalIntegrityError(
                    "journal event count does not match expectation"
                )

        if expected_head_hash is not None:
            if _hash(
                expected_head_hash,
                "expected_head_hash",
            ) != journal.head_hash:
                raise JournalIntegrityError(
                    "journal head hash does not match expectation"
                )

        if expected_journal_hash is not None:
            if _hash(
                expected_journal_hash,
                "expected_journal_hash",
            ) != computed.journal_hash:
                raise JournalIntegrityError(
                    "journal hash does not match expectation"
                )

        return journal

    @classmethod
    def from_events(
        cls,
        plan_id: str,
        events: Iterable[ExecutionEvent],
    ) -> "ExecutionJournal":
        if isinstance(events, (str, bytes)):
            raise ExecutionJournalError(
                "events must be an iterable of ExecutionEvent"
            )
        journal = cls(plan_id)
        for event in events:
            journal.append_event(event)
        return journal
