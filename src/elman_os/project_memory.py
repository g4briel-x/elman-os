"""Structured, append-only project memory for ELMAN-OS v0.7.

The store keeps immutable revision metadata in SQLite and separates payloads
from their integrity records. Retention can therefore remove expired payloads
without erasing provenance, hashes, or the fact that a revision existed.

This boundary is deliberately local and deterministic. It performs no network
access, never reads environment secrets, and rejects obvious secret material
before a payload reaches persistent storage.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Iterator

from .agent_contracts import canonical_json


PROJECT_MEMORY_FORMAT_VERSION: Final[int] = 1
PROJECT_MEMORY_MAX_PAYLOAD_BYTES: Final[int] = 1_048_576

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:api[_-]?key|authorization|client[_-]?secret|cookie|"
    r"credential|password|passwd|private[_-]?key|secret|session|token)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)
_SENSITIVE_VALUES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|password|passwd|secret|token)"
        r"\s*[:=]\s*[^\s,;]{8,}"
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
)


class ProjectMemoryError(ValueError):
    """A project-memory contract or operation is invalid."""


class ProjectMemoryIntegrityError(ProjectMemoryError):
    """Persisted or serialized project memory failed integrity validation."""


class ProjectMemoryConflictError(ProjectMemoryError):
    """A concurrent or duplicate write conflicts with current memory state."""


class ProjectMemorySecretError(ProjectMemoryError):
    """A payload contains a forbidden secret key or credential pattern."""


class ImmutableDecisionError(ProjectMemoryError):
    """An operation attempted to revise a decision in place."""


class ProjectMemoryKind(StrEnum):
    DECISION = "decision"
    CONSTRAINT = "constraint"
    CONVENTION = "convention"
    TEST_RESULT = "test-result"
    MIGRATION = "migration"
    INCIDENT = "incident"
    SOURCE_OF_TRUTH = "source-of-truth"


class ProjectMemoryState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    OBSOLETE = "obsolete"


class ProjectMemoryRetentionClass(StrEnum):
    PERMANENT = "permanent"
    PROJECT = "project"
    EXECUTION = "execution"
    TRANSIENT = "transient"


class ProjectMemorySourceType(StrEnum):
    USER_APPROVAL = "user-approval"
    EXECUTION = "execution"
    TEST_RUN = "test-run"
    MIGRATION = "migration"
    INCIDENT = "incident"
    POLICY = "policy"
    SYSTEM = "system"


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectMemoryError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ProjectMemoryError(f"{name} exceeds {maximum} characters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in result):
        raise ProjectMemoryError(f"{name} contains control characters")
    return result


def _identifier(value: object, name: str) -> str:
    result = _text(value, name, maximum=192)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ProjectMemoryError(f"{name} has an invalid format")
    return result


def _optional_identifier(value: object | None, name: str) -> str | None:
    return None if value is None else _identifier(value, name)


def _hash(value: object, name: str) -> str:
    result = _text(value, name, maximum=64)
    if _SHA256.fullmatch(result) is None:
        raise ProjectMemoryError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ProjectMemoryError(f"{name} must be ISO-8601 UTC ending in Z")
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ProjectMemoryError(f"{name} is not valid ISO-8601 UTC") from exc
    else:
        raise ProjectMemoryError(f"{name} must be a UTC datetime or string")
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ProjectMemoryError(f"{name} must already be UTC")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _tokens(values: Iterable[object], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProjectMemoryError(f"{name} must be an iterable of tokens")
    result: set[str] = set()
    for raw in values:
        token = _text(raw, name, maximum=64)
        if _TOKEN.fullmatch(token) is None:
            raise ProjectMemoryError(f"{name} contains an invalid token")
        result.add(token)
    return tuple(sorted(result))


def _json_copy(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectMemoryError(f"{name} must be a JSON object")
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ProjectMemoryError(f"{name} must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > PROJECT_MEMORY_MAX_PAYLOAD_BYTES:
        raise ProjectMemoryError(
            f"{name} exceeds {PROJECT_MEMORY_MAX_PAYLOAD_BYTES} bytes"
        )
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ProjectMemoryError(f"{name} must be a JSON object")
    return decoded


def _scan_for_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ProjectMemoryError(f"{path} contains a non-string key")
            if _SENSITIVE_KEY.search(raw_key):
                raise ProjectMemorySecretError(
                    f"{path}.{raw_key} uses a forbidden sensitive key"
                )
            _scan_for_secrets(child, f"{path}.{raw_key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_for_secrets(child, f"{path}[{index}]")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ProjectMemoryError(f"{path} contains a non-finite number")
    if isinstance(value, str):
        for pattern in _SENSITIVE_VALUES:
            if pattern.search(value):
                raise ProjectMemorySecretError(
                    f"{path} contains a forbidden credential pattern"
                )


def _payload_document(
    *, title: str, content: Mapping[str, Any], labels: tuple[str, ...]
) -> dict[str, Any]:
    document = {
        "title": _text(title, "title", maximum=512),
        "content": _json_copy(content, "content"),
        "labels": list(labels),
    }
    _scan_for_secrets(document)
    encoded = canonical_json(document)
    if len(encoded.encode("utf-8")) > PROJECT_MEMORY_MAX_PAYLOAD_BYTES:
        raise ProjectMemoryError(
            f"payload exceeds {PROJECT_MEMORY_MAX_PAYLOAD_BYTES} bytes"
        )
    return document


def _search_text(document: Mapping[str, Any]) -> str:
    return canonical_json(document).casefold()


@dataclass(frozen=True, slots=True)
class ProjectMemoryOrigin:
    """Immutable provenance attached to one memory revision."""

    source_type: ProjectMemorySourceType
    source_id: str
    actor_id: str
    captured_at: str
    evidence_references: tuple[str, ...] = ()
    version: int = PROJECT_MEMORY_FORMAT_VERSION

    def __post_init__(self) -> None:
        try:
            source_type = ProjectMemorySourceType(self.source_type)
        except (TypeError, ValueError) as exc:
            raise ProjectMemoryError("source_type is invalid") from exc
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "actor_id", _identifier(self.actor_id, "actor_id"))
        object.__setattr__(
            self, "captured_at", _timestamp(self.captured_at, "captured_at")
        )
        evidence = tuple(
            dict.fromkeys(
                _identifier(value, "evidence_references")
                for value in self.evidence_references
            )
        )
        object.__setattr__(self, "evidence_references", evidence)
        _scan_for_secrets(
            {
                "source_id": self.source_id,
                "actor_id": self.actor_id,
                "evidence_references": list(evidence),
            },
            "origin",
        )
        if self.version != PROJECT_MEMORY_FORMAT_VERSION:
            raise ProjectMemoryError("unsupported project-memory origin version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "project_memory_origin",
            "version": self.version,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "actor_id": self.actor_id,
            "captured_at": self.captured_at,
            "evidence_references": list(self.evidence_references),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectMemoryOrigin":
        if data.get("record_type") != "project_memory_origin":
            raise ProjectMemoryError("record_type must be project_memory_origin")
        return cls(
            source_type=ProjectMemorySourceType(data["source_type"]),
            source_id=data["source_id"],
            actor_id=data["actor_id"],
            captured_at=data["captured_at"],
            evidence_references=tuple(data.get("evidence_references", ())),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ProjectMemoryOrigin":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProjectMemoryError("project-memory origin JSON is invalid") from exc
        if not isinstance(data, dict):
            raise ProjectMemoryError("project-memory origin JSON must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ProjectMemoryRetentionPolicy:
    """Fail-closed retention durations for execution and transient payloads."""

    policy_id: str
    execution_days: int = 90
    transient_days: int = 7
    maximum_query_results: int = 500
    fail_closed: bool = True
    version: int = PROJECT_MEMORY_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        for name, minimum, maximum in (
            ("execution_days", 1, 3650),
            ("transient_days", 1, 365),
            ("maximum_query_results", 1, 1000),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProjectMemoryError(f"{name} must be an integer")
            if value < minimum or value > maximum:
                raise ProjectMemoryError(
                    f"{name} must be between {minimum} and {maximum}"
                )
        if self.fail_closed is not True:
            raise ProjectMemoryError("project-memory retention must fail closed")
        if self.version != PROJECT_MEMORY_FORMAT_VERSION:
            raise ProjectMemoryError("unsupported retention-policy version")

    def expires_at(
        self,
        retention_class: ProjectMemoryRetentionClass,
        captured_at: str,
    ) -> str | None:
        if retention_class in {
            ProjectMemoryRetentionClass.PERMANENT,
            ProjectMemoryRetentionClass.PROJECT,
        }:
            return None
        days = (
            self.execution_days
            if retention_class is ProjectMemoryRetentionClass.EXECUTION
            else self.transient_days
        )
        return _timestamp(
            _parse_timestamp(captured_at) + timedelta(days=days),
            "expires_at",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "project_memory_retention_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "execution_days": self.execution_days,
            "transient_days": self.transient_days,
            "maximum_query_results": self.maximum_query_results,
            "fail_closed": self.fail_closed,
        }

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())


@dataclass(frozen=True, slots=True)
class ProjectMemoryRecord:
    """One hash-bound revision, optionally accompanied by its live payload."""

    tenant_id: str
    project_id: str
    memory_id: str
    revision: int
    execution_id: str | None
    kind: ProjectMemoryKind
    state: ProjectMemoryState
    retention_class: ProjectMemoryRetentionClass
    expires_at: str | None
    supersedes_memory_id: str | None
    origin: ProjectMemoryOrigin
    payload_hash: str
    previous_revision_hash: str | None
    revision_hash: str
    recorded_at: str
    title: str | None = None
    content_json: str | None = None
    labels: tuple[str, ...] = ()
    version: int = PROJECT_MEMORY_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        object.__setattr__(self, "memory_id", _identifier(self.memory_id, "memory_id"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ProjectMemoryError("revision must be an integer")
        if self.revision < 1:
            raise ProjectMemoryError("revision must be positive")
        object.__setattr__(
            self, "execution_id", _optional_identifier(self.execution_id, "execution_id")
        )
        try:
            object.__setattr__(self, "kind", ProjectMemoryKind(self.kind))
            object.__setattr__(self, "state", ProjectMemoryState(self.state))
            object.__setattr__(
                self,
                "retention_class",
                ProjectMemoryRetentionClass(self.retention_class),
            )
        except (TypeError, ValueError) as exc:
            raise ProjectMemoryError("memory enum value is invalid") from exc
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        object.__setattr__(
            self,
            "supersedes_memory_id",
            _optional_identifier(self.supersedes_memory_id, "supersedes_memory_id"),
        )
        if not isinstance(self.origin, ProjectMemoryOrigin):
            raise ProjectMemoryError("origin must be ProjectMemoryOrigin")
        object.__setattr__(self, "payload_hash", _hash(self.payload_hash, "payload_hash"))
        if self.previous_revision_hash is not None:
            object.__setattr__(
                self,
                "previous_revision_hash",
                _hash(self.previous_revision_hash, "previous_revision_hash"),
            )
        object.__setattr__(self, "revision_hash", _hash(self.revision_hash, "revision_hash"))
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at, "recorded_at"))
        if self.recorded_at != self.origin.captured_at:
            raise ProjectMemoryIntegrityError(
                "recorded_at must equal origin.captured_at"
            )
        if self.version != PROJECT_MEMORY_FORMAT_VERSION:
            raise ProjectMemoryError("unsupported project-memory record version")
        if self.content_json is None:
            if self.title is not None or self.labels:
                raise ProjectMemoryIntegrityError(
                    "purged records cannot expose partial payload fields"
                )
        else:
            try:
                content = json.loads(self.content_json)
            except json.JSONDecodeError as exc:
                raise ProjectMemoryIntegrityError("content_json is invalid") from exc
            if not isinstance(content, dict) or self.title is None:
                raise ProjectMemoryIntegrityError("live payload is incomplete")
            labels = _tokens(self.labels, "labels")
            object.__setattr__(self, "labels", labels)
            document = _payload_document(
                title=self.title,
                content=content,
                labels=labels,
            )
            if _sha256_document(document) != self.payload_hash:
                raise ProjectMemoryIntegrityError(
                    "payload_hash does not match the live payload"
                )
            object.__setattr__(self, "title", document["title"])
            object.__setattr__(
                self, "content_json", canonical_json(document["content"])
            )
        self.verify_hash()

    @property
    def payload_available(self) -> bool:
        return self.content_json is not None

    @property
    def content(self) -> Mapping[str, Any] | None:
        if self.content_json is None:
            return None
        data = json.loads(self.content_json)
        return MappingProxyType(data)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "project_memory_revision",
            "version": self.version,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "memory_id": self.memory_id,
            "revision": self.revision,
            "execution_id": self.execution_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "retention_class": self.retention_class.value,
            "expires_at": self.expires_at,
            "supersedes_memory_id": self.supersedes_memory_id,
            "origin": self.origin.to_dict(),
            "payload_hash": self.payload_hash,
            "previous_revision_hash": self.previous_revision_hash,
            "recorded_at": self.recorded_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.revision_hash != self.compute_hash():
            raise ProjectMemoryIntegrityError(
                "revision_hash does not match revision metadata"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["revision_hash"] = self.revision_hash
        data["payload_available"] = self.payload_available
        data["payload"] = (
            None
            if not self.payload_available
            else {
                "title": self.title,
                "content": json.loads(self.content_json or "{}"),
                "labels": list(self.labels),
            }
        )
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectMemoryRecord":
        if data.get("record_type") != "project_memory_revision":
            raise ProjectMemoryError("record_type must be project_memory_revision")
        payload = data.get("payload")
        if payload is None:
            title = None
            content_json = None
            labels: tuple[str, ...] = ()
        else:
            if not isinstance(payload, Mapping):
                raise ProjectMemoryError("payload must be an object or null")
            title = payload["title"]
            content_json = canonical_json(payload["content"])
            labels = tuple(payload.get("labels", ()))
        if data.get("payload_available") is not (payload is not None):
            raise ProjectMemoryIntegrityError(
                "payload_available does not match serialized payload"
            )
        return cls(
            tenant_id=data["tenant_id"],
            project_id=data["project_id"],
            memory_id=data["memory_id"],
            revision=data["revision"],
            execution_id=data.get("execution_id"),
            kind=ProjectMemoryKind(data["kind"]),
            state=ProjectMemoryState(data["state"]),
            retention_class=ProjectMemoryRetentionClass(
                data["retention_class"]
            ),
            expires_at=data.get("expires_at"),
            supersedes_memory_id=data.get("supersedes_memory_id"),
            origin=ProjectMemoryOrigin.from_dict(data["origin"]),
            payload_hash=data["payload_hash"],
            previous_revision_hash=data.get("previous_revision_hash"),
            revision_hash=data["revision_hash"],
            recorded_at=data["recorded_at"],
            title=title,
            content_json=content_json,
            labels=labels,
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ProjectMemoryRecord":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProjectMemoryError("project-memory record JSON is invalid") from exc
        if not isinstance(data, dict):
            raise ProjectMemoryError("project-memory record JSON must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ProjectMemoryRetentionEvent:
    event_id: str
    tenant_id: str
    project_id: str
    memory_id: str
    revision: int
    payload_hash: str
    purged_at: str
    policy_id: str
    event_hash: str
    version: int = PROJECT_MEMORY_FORMAT_VERSION

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "project_memory_retention_event",
            "version": self.version,
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "memory_id": self.memory_id,
            "revision": self.revision,
            "payload_hash": self.payload_hash,
            "purged_at": self.purged_at,
            "policy_id": self.policy_id,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.event_hash != self.compute_hash():
            raise ProjectMemoryIntegrityError(
                "retention event hash does not match its metadata"
            )

    def __post_init__(self) -> None:
        for name in ("event_id", "tenant_id", "project_id", "memory_id", "policy_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ProjectMemoryError("revision must be an integer")
        if self.revision < 1:
            raise ProjectMemoryError("revision must be positive")
        object.__setattr__(self, "payload_hash", _hash(self.payload_hash, "payload_hash"))
        object.__setattr__(self, "purged_at", _timestamp(self.purged_at, "purged_at"))
        object.__setattr__(self, "event_hash", _hash(self.event_hash, "event_hash"))
        if self.version != PROJECT_MEMORY_FORMAT_VERSION:
            raise ProjectMemoryError("unsupported retention-event version")
        self.verify_hash()

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["event_hash"] = self.event_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class ProjectMemoryRetentionReport:
    tenant_id: str
    purged_at: str
    policy_id: str
    events: tuple[ProjectMemoryRetentionEvent, ...]
    report_hash: str
    version: int = PROJECT_MEMORY_FORMAT_VERSION

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "project_memory_retention_report",
            "version": self.version,
            "tenant_id": self.tenant_id,
            "purged_at": self.purged_at,
            "policy_id": self.policy_id,
            "event_hashes": [event.event_hash for event in self.events],
        }

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "purged_at", _timestamp(self.purged_at, "purged_at"))
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        events = tuple(self.events)
        if not all(isinstance(event, ProjectMemoryRetentionEvent) for event in events):
            raise ProjectMemoryError("events must contain retention events")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "report_hash", _hash(self.report_hash, "report_hash"))
        if self.version != PROJECT_MEMORY_FORMAT_VERSION:
            raise ProjectMemoryError("unsupported retention-report version")
        if self.report_hash != _sha256_document(self.hash_material()):
            raise ProjectMemoryIntegrityError("retention report hash is invalid")

    @property
    def purged_payload_count(self) -> int:
        return len(self.events)

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["events"] = [event.to_dict() for event in self.events]
        data["purged_payload_count"] = self.purged_payload_count
        data["report_hash"] = self.report_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


class ProjectMemoryStore:
    """SQLite-backed append-only memory scoped by tenant and project."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_policy: ProjectMemoryRetentionPolicy | None = None,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if str(database_path) == ":memory:":
            raise ProjectMemoryError(
                "project memory requires a durable file-backed SQLite database"
            )
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_policy = retention_policy or ProjectMemoryRetentionPolicy(
            policy_id="policy:project-memory-default-v1"
        )
        if not isinstance(self.retention_policy, ProjectMemoryRetentionPolicy):
            raise ProjectMemoryError("retention_policy is invalid")
        if busy_timeout_seconds <= 0 or busy_timeout_seconds > 60:
            raise ProjectMemoryError("busy_timeout_seconds must be in (0, 60]")
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._initialize()
        if os.name != "nt":
            self.database_path.chmod(0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS project_memory_schema (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        schema_version INTEGER NOT NULL
                    );
                    INSERT OR IGNORE INTO project_memory_schema (
                        singleton, schema_version
                    ) VALUES (1, 1);

                    CREATE TABLE IF NOT EXISTS project_memory_revisions (
                        tenant_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        execution_id TEXT,
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        retention_class TEXT NOT NULL,
                        expires_at TEXT,
                        supersedes_memory_id TEXT,
                        origin_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        previous_revision_hash TEXT,
                        revision_hash TEXT NOT NULL UNIQUE,
                        recorded_at TEXT NOT NULL,
                        format_version INTEGER NOT NULL,
                        PRIMARY KEY (tenant_id, project_id, memory_id, revision)
                    );

                    CREATE TABLE IF NOT EXISTS project_memory_payloads (
                        tenant_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        content_json TEXT NOT NULL,
                        labels_json TEXT NOT NULL,
                        search_text TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, project_id, memory_id, revision),
                        FOREIGN KEY (tenant_id, project_id, memory_id, revision)
                            REFERENCES project_memory_revisions (
                                tenant_id, project_id, memory_id, revision
                            )
                    );

                    CREATE TABLE IF NOT EXISTS project_memory_retention_events (
                        event_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        payload_hash TEXT NOT NULL,
                        purged_at TEXT NOT NULL,
                        policy_id TEXT NOT NULL,
                        event_hash TEXT NOT NULL UNIQUE,
                        format_version INTEGER NOT NULL,
                        UNIQUE (tenant_id, project_id, memory_id, revision)
                    );

                    CREATE INDEX IF NOT EXISTS idx_project_memory_scope
                        ON project_memory_revisions (
                            tenant_id, project_id, execution_id, kind,
                            recorded_at, memory_id, revision
                        );
                    CREATE INDEX IF NOT EXISTS idx_project_memory_expiration
                        ON project_memory_revisions (
                            tenant_id, expires_at, memory_id, revision
                        );

                    CREATE TRIGGER IF NOT EXISTS project_memory_revisions_no_update
                    BEFORE UPDATE ON project_memory_revisions
                    BEGIN
                        SELECT RAISE(ABORT, 'project memory revisions are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_revisions_no_delete
                    BEFORE DELETE ON project_memory_revisions
                    BEGIN
                        SELECT RAISE(ABORT, 'project memory revisions are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_payloads_no_update
                    BEFORE UPDATE ON project_memory_payloads
                    BEGIN
                        SELECT RAISE(ABORT, 'project memory payloads are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_retention_no_update
                    BEFORE UPDATE ON project_memory_retention_events
                    BEGIN
                        SELECT RAISE(ABORT, 'retention events are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_retention_no_delete
                    BEFORE DELETE ON project_memory_retention_events
                    BEGIN
                        SELECT RAISE(ABORT, 'retention events are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_schema_no_update
                    BEFORE UPDATE ON project_memory_schema
                    BEGIN
                        SELECT RAISE(ABORT, 'project memory schema is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS project_memory_schema_no_delete
                    BEFORE DELETE ON project_memory_schema
                    BEGIN
                        SELECT RAISE(ABORT, 'project memory schema is immutable');
                    END;
                    """
                )
                schema = connection.execute(
                    "SELECT schema_version FROM project_memory_schema WHERE singleton = 1"
                ).fetchone()
                if schema is None or schema["schema_version"] != (
                    PROJECT_MEMORY_FORMAT_VERSION
                ):
                    raise ProjectMemoryError(
                        "unsupported project-memory database schema"
                    )
        except sqlite3.Error as exc:
            raise ProjectMemoryError(
                "project-memory database initialization failed"
            ) from exc

    def record(
        self,
        *,
        tenant_id: str,
        project_id: str,
        kind: ProjectMemoryKind,
        title: str,
        content: Mapping[str, Any],
        origin: ProjectMemoryOrigin,
        execution_id: str | None = None,
        labels: Iterable[str] = (),
        retention_class: ProjectMemoryRetentionClass = (
            ProjectMemoryRetentionClass.PROJECT
        ),
        supersedes_memory_id: str | None = None,
    ) -> ProjectMemoryRecord:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _identifier(project_id, "project_id")
        execution = _optional_identifier(execution_id, "execution_id")
        try:
            memory_kind = ProjectMemoryKind(kind)
            retention = ProjectMemoryRetentionClass(retention_class)
        except (TypeError, ValueError) as exc:
            raise ProjectMemoryError("kind or retention_class is invalid") from exc
        if not isinstance(origin, ProjectMemoryOrigin):
            raise ProjectMemoryError("origin must be ProjectMemoryOrigin")
        if memory_kind is ProjectMemoryKind.DECISION and retention is not (
            ProjectMemoryRetentionClass.PERMANENT
        ):
            raise ProjectMemoryError("decisions must use permanent retention")
        if memory_kind is ProjectMemoryKind.DECISION and (
            origin.source_type is not ProjectMemorySourceType.USER_APPROVAL
            or not origin.evidence_references
        ):
            raise ProjectMemoryError(
                "decisions require human-approval provenance and evidence"
            )
        if memory_kind in {
            ProjectMemoryKind.CONSTRAINT,
            ProjectMemoryKind.CONVENTION,
            ProjectMemoryKind.MIGRATION,
            ProjectMemoryKind.SOURCE_OF_TRUTH,
        } and retention not in {
            ProjectMemoryRetentionClass.PERMANENT,
            ProjectMemoryRetentionClass.PROJECT,
        }:
            raise ProjectMemoryError(
                "durable project knowledge cannot use expiring retention"
            )
        if retention is ProjectMemoryRetentionClass.EXECUTION and execution is None:
            raise ProjectMemoryError("execution retention requires execution_id")
        _scan_for_secrets(
            {
                "tenant_id": tenant,
                "project_id": project,
                "execution_id": execution,
                "origin": origin.to_dict(),
            },
            "memory_metadata",
        )
        normalized_labels = _tokens(labels, "labels")
        payload = _payload_document(
            title=title,
            content=content,
            labels=normalized_labels,
        )
        payload_hash = _sha256_document(payload)
        supersedes = _optional_identifier(
            supersedes_memory_id, "supersedes_memory_id"
        )
        identity = {
            "tenant_id": tenant,
            "project_id": project,
            "execution_id": execution,
            "kind": memory_kind.value,
            "origin": origin.to_dict(),
            "payload_hash": payload_hash,
            "supersedes_memory_id": supersedes,
        }
        memory_id = f"memory:{_sha256_document(identity)}"
        expires_at = self.retention_policy.expires_at(
            retention, origin.captured_at
        )
        metadata = {
            "record_type": "project_memory_revision",
            "version": PROJECT_MEMORY_FORMAT_VERSION,
            "tenant_id": tenant,
            "project_id": project,
            "memory_id": memory_id,
            "revision": 1,
            "execution_id": execution,
            "kind": memory_kind.value,
            "state": ProjectMemoryState.ACTIVE.value,
            "retention_class": retention.value,
            "expires_at": expires_at,
            "supersedes_memory_id": supersedes,
            "origin": origin.to_dict(),
            "payload_hash": payload_hash,
            "previous_revision_hash": None,
            "recorded_at": origin.captured_at,
        }
        revision_hash = _sha256_document(metadata)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if supersedes is not None:
                    previous = self._latest_row(
                        connection, tenant, project, supersedes
                    )
                    if previous is None:
                        raise ProjectMemoryError(
                            "supersedes_memory_id does not exist in this project"
                        )
                    if ProjectMemoryKind(previous["kind"]) is not memory_kind:
                        raise ProjectMemoryError(
                            "a memory can only supersede the same memory kind"
                        )
                self._insert_revision(
                    connection=connection,
                    metadata=metadata,
                    revision_hash=revision_hash,
                    payload=payload,
                )
                connection.commit()
        except ProjectMemoryError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ProjectMemoryConflictError(
                "project memory already contains this immutable record"
            ) from exc
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory write failed") from exc
        record = self.get(
            tenant_id=tenant,
            project_id=project,
            memory_id=memory_id,
        )
        if record is None:
            raise ProjectMemoryIntegrityError("stored memory cannot be read back")
        return record

    def revise(
        self,
        *,
        tenant_id: str,
        project_id: str,
        memory_id: str,
        expected_revision: int,
        title: str,
        content: Mapping[str, Any],
        origin: ProjectMemoryOrigin,
        labels: Iterable[str] = (),
        state: ProjectMemoryState = ProjectMemoryState.ACTIVE,
        retention_class: ProjectMemoryRetentionClass | None = None,
    ) -> ProjectMemoryRecord:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _identifier(project_id, "project_id")
        identifier = _identifier(memory_id, "memory_id")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ProjectMemoryError("expected_revision must be an integer")
        if expected_revision < 1:
            raise ProjectMemoryError("expected_revision must be positive")
        if not isinstance(origin, ProjectMemoryOrigin):
            raise ProjectMemoryError("origin must be ProjectMemoryOrigin")
        try:
            next_state = ProjectMemoryState(state)
        except (TypeError, ValueError) as exc:
            raise ProjectMemoryError("state is invalid") from exc
        normalized_labels = _tokens(labels, "labels")
        payload = _payload_document(
            title=title,
            content=content,
            labels=normalized_labels,
        )
        payload_hash = _sha256_document(payload)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = self._latest_row(
                    connection, tenant, project, identifier
                )
                if previous is None:
                    raise ProjectMemoryError("memory_id does not exist")
                if previous["revision"] != expected_revision:
                    raise ProjectMemoryConflictError(
                        "expected_revision does not match current memory"
                    )
                kind = ProjectMemoryKind(previous["kind"])
                if kind is ProjectMemoryKind.DECISION:
                    raise ImmutableDecisionError(
                        "decisions cannot be revised; record a superseding decision"
                    )
                previous_state = ProjectMemoryState(previous["state"])
                if previous_state is not ProjectMemoryState.ACTIVE:
                    raise ProjectMemoryConflictError(
                        "obsolete or superseded memory cannot be revised"
                    )
                if origin.captured_at < previous["recorded_at"]:
                    raise ProjectMemoryError(
                        "revision provenance cannot precede the previous revision"
                    )
                retention = (
                    ProjectMemoryRetentionClass(previous["retention_class"])
                    if retention_class is None
                    else ProjectMemoryRetentionClass(retention_class)
                )
                execution = previous["execution_id"]
                if (
                    retention is ProjectMemoryRetentionClass.EXECUTION
                    and execution is None
                ):
                    raise ProjectMemoryError(
                        "execution retention requires execution_id"
                    )
                expires_at = self.retention_policy.expires_at(
                    retention, origin.captured_at
                )
                metadata = {
                    "record_type": "project_memory_revision",
                    "version": PROJECT_MEMORY_FORMAT_VERSION,
                    "tenant_id": tenant,
                    "project_id": project,
                    "memory_id": identifier,
                    "revision": expected_revision + 1,
                    "execution_id": execution,
                    "kind": kind.value,
                    "state": next_state.value,
                    "retention_class": retention.value,
                    "expires_at": expires_at,
                    "supersedes_memory_id": previous["supersedes_memory_id"],
                    "origin": origin.to_dict(),
                    "payload_hash": payload_hash,
                    "previous_revision_hash": previous["revision_hash"],
                    "recorded_at": origin.captured_at,
                }
                revision_hash = _sha256_document(metadata)
                self._insert_revision(
                    connection=connection,
                    metadata=metadata,
                    revision_hash=revision_hash,
                    payload=payload,
                )
                connection.commit()
        except (ProjectMemoryError, ValueError):
            raise
        except sqlite3.IntegrityError as exc:
            raise ProjectMemoryConflictError(
                "concurrent project-memory revision conflict"
            ) from exc
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory revision failed") from exc
        record = self.get(
            tenant_id=tenant,
            project_id=project,
            memory_id=identifier,
        )
        if record is None:
            raise ProjectMemoryIntegrityError("revised memory cannot be read back")
        return record

    def mark_obsolete(
        self,
        *,
        tenant_id: str,
        project_id: str,
        memory_id: str,
        expected_revision: int,
        origin: ProjectMemoryOrigin,
    ) -> ProjectMemoryRecord:
        current = self.get(
            tenant_id=tenant_id,
            project_id=project_id,
            memory_id=memory_id,
        )
        if current is None:
            raise ProjectMemoryError("memory_id does not exist")
        if not current.payload_available or current.content is None or current.title is None:
            raise ProjectMemoryConflictError(
                "memory with a purged payload cannot be revised"
            )
        return self.revise(
            tenant_id=tenant_id,
            project_id=project_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
            title=current.title,
            content=current.content,
            origin=origin,
            labels=current.labels,
            state=ProjectMemoryState.OBSOLETE,
        )

    def get(
        self,
        *,
        tenant_id: str,
        project_id: str,
        memory_id: str,
        revision: int | None = None,
    ) -> ProjectMemoryRecord | None:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _identifier(project_id, "project_id")
        identifier = _identifier(memory_id, "memory_id")
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise ProjectMemoryError("revision must be a positive integer")
        query = self._record_select() + """
            WHERE r.tenant_id = ? AND r.project_id = ? AND r.memory_id = ?
        """
        parameters: list[object] = [tenant, project, identifier]
        if revision is None:
            query += " ORDER BY r.revision DESC LIMIT 1"
        else:
            query += " AND r.revision = ?"
            parameters.append(revision)
        try:
            with self._connection() as connection:
                row = connection.execute(query, tuple(parameters)).fetchone()
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory read failed") from exc
        return None if row is None else self._decode_record(row)

    def history(
        self,
        *,
        tenant_id: str,
        project_id: str,
        memory_id: str,
    ) -> tuple[ProjectMemoryRecord, ...]:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _identifier(project_id, "project_id")
        identifier = _identifier(memory_id, "memory_id")
        query = self._record_select() + """
            WHERE r.tenant_id = ? AND r.project_id = ? AND r.memory_id = ?
            ORDER BY r.revision
        """
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    query, (tenant, project, identifier)
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory history read failed") from exc
        records = tuple(self._decode_record(row) for row in rows)
        for index, record in enumerate(records):
            expected_previous = None if index == 0 else records[index - 1].revision_hash
            if record.previous_revision_hash != expected_previous:
                raise ProjectMemoryIntegrityError(
                    "project-memory revision chain is broken"
                )
        return records

    def search(
        self,
        *,
        tenant_id: str,
        project_id: str,
        execution_id: str | None = None,
        kinds: Iterable[ProjectMemoryKind] = (),
        query: str | None = None,
        include_inactive: bool = False,
        include_superseded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ProjectMemoryRecord, ...]:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _identifier(project_id, "project_id")
        execution = _optional_identifier(execution_id, "execution_id")
        normalized_kinds = tuple(dict.fromkeys(ProjectMemoryKind(kind) for kind in kinds))
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ProjectMemoryError("limit must be an integer")
        if limit < 1 or limit > self.retention_policy.maximum_query_results:
            raise ProjectMemoryError(
                "limit exceeds the configured maximum query result count"
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ProjectMemoryError("offset must be a non-negative integer")
        normalized_query = None
        if query is not None:
            normalized_query = _text(query, "query", maximum=512).casefold()
        sql = """
            WITH latest AS (
                SELECT tenant_id, project_id, memory_id, MAX(revision) AS revision
                FROM project_memory_revisions
                WHERE tenant_id = ? AND project_id = ?
                GROUP BY tenant_id, project_id, memory_id
            )
        """ + self._record_select() + """
            JOIN latest l ON l.tenant_id = r.tenant_id
                AND l.project_id = r.project_id
                AND l.memory_id = r.memory_id
                AND l.revision = r.revision
            WHERE r.tenant_id = ? AND r.project_id = ?
        """
        parameters: list[object] = [tenant, project, tenant, project]
        if execution is not None:
            sql += " AND r.execution_id = ?"
            parameters.append(execution)
        if normalized_kinds:
            placeholders = ",".join("?" for _ in normalized_kinds)
            sql += f" AND r.kind IN ({placeholders})"
            parameters.extend(kind.value for kind in normalized_kinds)
        if not include_inactive:
            sql += " AND r.state = ?"
            parameters.append(ProjectMemoryState.ACTIVE.value)
        if not include_superseded:
            sql += """
                AND NOT EXISTS (
                    SELECT 1
                    FROM project_memory_revisions successor
                    JOIN (
                        SELECT tenant_id, project_id, memory_id,
                               MAX(revision) AS revision
                        FROM project_memory_revisions
                        WHERE tenant_id = ? AND project_id = ?
                        GROUP BY tenant_id, project_id, memory_id
                    ) successor_latest
                      ON successor_latest.tenant_id = successor.tenant_id
                     AND successor_latest.project_id = successor.project_id
                     AND successor_latest.memory_id = successor.memory_id
                     AND successor_latest.revision = successor.revision
                    WHERE successor.tenant_id = r.tenant_id
                      AND successor.project_id = r.project_id
                      AND successor.supersedes_memory_id = r.memory_id
                )
            """
            parameters.extend([tenant, project])
        if normalized_query is not None:
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            sql += " AND p.search_text LIKE ? ESCAPE '\\'"
            parameters.append(f"%{escaped}%")
        sql += " ORDER BY r.recorded_at DESC, r.memory_id, r.revision DESC LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
        try:
            with self._connection() as connection:
                rows = connection.execute(sql, tuple(parameters)).fetchall()
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory search failed") from exc
        return tuple(self._decode_record(row) for row in rows)

    def apply_retention(
        self,
        *,
        tenant_id: str,
        as_of: str | datetime,
    ) -> ProjectMemoryRetentionReport:
        tenant = _identifier(tenant_id, "tenant_id")
        purged_at = _timestamp(as_of, "as_of")
        events: list[ProjectMemoryRetentionEvent] = []
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """
                    SELECT r.tenant_id, r.project_id, r.memory_id, r.revision,
                           r.kind, r.payload_hash
                    FROM project_memory_revisions r
                    JOIN project_memory_payloads p
                      ON p.tenant_id = r.tenant_id
                     AND p.project_id = r.project_id
                     AND p.memory_id = r.memory_id
                     AND p.revision = r.revision
                    WHERE r.tenant_id = ?
                      AND r.expires_at IS NOT NULL
                      AND r.expires_at <= ?
                    ORDER BY r.project_id, r.memory_id, r.revision
                    """,
                    (tenant, purged_at),
                ).fetchall()
                for row in rows:
                    if ProjectMemoryKind(row["kind"]) is ProjectMemoryKind.DECISION:
                        raise ProjectMemoryIntegrityError(
                            "a decision unexpectedly became eligible for retention"
                        )
                    material = {
                        "tenant_id": row["tenant_id"],
                        "project_id": row["project_id"],
                        "memory_id": row["memory_id"],
                        "revision": row["revision"],
                        "payload_hash": row["payload_hash"],
                        "purged_at": purged_at,
                        "policy_id": self.retention_policy.policy_id,
                    }
                    event_id = f"retention:{_sha256_document(material)}"
                    event_material = {
                        "record_type": "project_memory_retention_event",
                        "version": PROJECT_MEMORY_FORMAT_VERSION,
                        "event_id": event_id,
                        **material,
                    }
                    event_hash = _sha256_document(event_material)
                    connection.execute(
                        """
                        INSERT INTO project_memory_retention_events (
                            event_id, tenant_id, project_id, memory_id,
                            revision, payload_hash, purged_at, policy_id,
                            event_hash, format_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            row["tenant_id"],
                            row["project_id"],
                            row["memory_id"],
                            row["revision"],
                            row["payload_hash"],
                            purged_at,
                            self.retention_policy.policy_id,
                            event_hash,
                            PROJECT_MEMORY_FORMAT_VERSION,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM project_memory_payloads
                        WHERE tenant_id = ? AND project_id = ?
                          AND memory_id = ? AND revision = ?
                        """,
                        (
                            row["tenant_id"],
                            row["project_id"],
                            row["memory_id"],
                            row["revision"],
                        ),
                    )
                    events.append(
                        ProjectMemoryRetentionEvent(
                            event_id=event_id,
                            tenant_id=row["tenant_id"],
                            project_id=row["project_id"],
                            memory_id=row["memory_id"],
                            revision=row["revision"],
                            payload_hash=row["payload_hash"],
                            purged_at=purged_at,
                            policy_id=self.retention_policy.policy_id,
                            event_hash=event_hash,
                        )
                    )
                connection.commit()
        except ProjectMemoryError:
            raise
        except sqlite3.Error as exc:
            raise ProjectMemoryError("project-memory retention failed") from exc
        report_material = {
            "record_type": "project_memory_retention_report",
            "version": PROJECT_MEMORY_FORMAT_VERSION,
            "tenant_id": tenant,
            "purged_at": purged_at,
            "policy_id": self.retention_policy.policy_id,
            "event_hashes": [event.event_hash for event in events],
        }
        return ProjectMemoryRetentionReport(
            tenant_id=tenant,
            purged_at=purged_at,
            policy_id=self.retention_policy.policy_id,
            events=tuple(events),
            report_hash=_sha256_document(report_material),
        )

    def retention_events(
        self,
        *,
        tenant_id: str,
        project_id: str | None = None,
    ) -> tuple[ProjectMemoryRetentionEvent, ...]:
        tenant = _identifier(tenant_id, "tenant_id")
        project = _optional_identifier(project_id, "project_id")
        sql = """
            SELECT event_id, tenant_id, project_id, memory_id, revision,
                   payload_hash, purged_at, policy_id, event_hash,
                   format_version
            FROM project_memory_retention_events
            WHERE tenant_id = ?
        """
        parameters: list[object] = [tenant]
        if project is not None:
            sql += " AND project_id = ?"
            parameters.append(project)
        sql += " ORDER BY purged_at, project_id, memory_id, revision"
        try:
            with self._connection() as connection:
                rows = connection.execute(sql, tuple(parameters)).fetchall()
        except sqlite3.Error as exc:
            raise ProjectMemoryError("retention-event read failed") from exc
        return tuple(
            ProjectMemoryRetentionEvent(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                memory_id=row["memory_id"],
                revision=row["revision"],
                payload_hash=row["payload_hash"],
                purged_at=row["purged_at"],
                policy_id=row["policy_id"],
                event_hash=row["event_hash"],
                version=row["format_version"],
            )
            for row in rows
        )

    @staticmethod
    def _record_select() -> str:
        return """
            SELECT r.tenant_id, r.project_id, r.memory_id, r.revision,
                   r.execution_id, r.kind, r.state, r.retention_class,
                   r.expires_at, r.supersedes_memory_id, r.origin_json,
                   r.payload_hash, r.previous_revision_hash, r.revision_hash,
                   r.recorded_at, r.format_version,
                   p.title, p.content_json, p.labels_json,
                   e.event_hash AS retention_event_hash
            FROM project_memory_revisions r
            LEFT JOIN project_memory_payloads p
              ON p.tenant_id = r.tenant_id
             AND p.project_id = r.project_id
             AND p.memory_id = r.memory_id
             AND p.revision = r.revision
            LEFT JOIN project_memory_retention_events e
              ON e.tenant_id = r.tenant_id
             AND e.project_id = r.project_id
             AND e.memory_id = r.memory_id
             AND e.revision = r.revision
        """

    @staticmethod
    def _latest_row(
        connection: sqlite3.Connection,
        tenant_id: str,
        project_id: str,
        memory_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT tenant_id, project_id, memory_id, revision, execution_id,
                   kind, state, retention_class, expires_at,
                   supersedes_memory_id, payload_hash, revision_hash,
                   recorded_at
            FROM project_memory_revisions
            WHERE tenant_id = ? AND project_id = ? AND memory_id = ?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (tenant_id, project_id, memory_id),
        ).fetchone()

    @staticmethod
    def _insert_revision(
        *,
        connection: sqlite3.Connection,
        metadata: Mapping[str, Any],
        revision_hash: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO project_memory_revisions (
                tenant_id, project_id, memory_id, revision, execution_id,
                kind, state, retention_class, expires_at,
                supersedes_memory_id, origin_json, payload_hash,
                previous_revision_hash, revision_hash, recorded_at,
                format_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["tenant_id"],
                metadata["project_id"],
                metadata["memory_id"],
                metadata["revision"],
                metadata["execution_id"],
                metadata["kind"],
                metadata["state"],
                metadata["retention_class"],
                metadata["expires_at"],
                metadata["supersedes_memory_id"],
                canonical_json(metadata["origin"]),
                metadata["payload_hash"],
                metadata["previous_revision_hash"],
                revision_hash,
                metadata["recorded_at"],
                metadata["version"],
            ),
        )
        connection.execute(
            """
            INSERT INTO project_memory_payloads (
                tenant_id, project_id, memory_id, revision, title,
                content_json, labels_json, search_text, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["tenant_id"],
                metadata["project_id"],
                metadata["memory_id"],
                metadata["revision"],
                payload["title"],
                canonical_json(payload["content"]),
                canonical_json({"labels": payload["labels"]}),
                _search_text(payload),
                metadata["payload_hash"],
            ),
        )

    @staticmethod
    def _decode_record(row: sqlite3.Row) -> ProjectMemoryRecord:
        try:
            payload_available = row["content_json"] is not None
            retention_recorded = row["retention_event_hash"] is not None
            if payload_available == retention_recorded:
                raise ProjectMemoryIntegrityError(
                    "payload presence does not match retention evidence"
                )
            origin = ProjectMemoryOrigin.from_json(row["origin_json"])
            labels: tuple[str, ...] = ()
            if row["labels_json"] is not None:
                label_data = json.loads(row["labels_json"])
                labels = tuple(label_data["labels"])
            return ProjectMemoryRecord(
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                memory_id=row["memory_id"],
                revision=row["revision"],
                execution_id=row["execution_id"],
                kind=ProjectMemoryKind(row["kind"]),
                state=ProjectMemoryState(row["state"]),
                retention_class=ProjectMemoryRetentionClass(
                    row["retention_class"]
                ),
                expires_at=row["expires_at"],
                supersedes_memory_id=row["supersedes_memory_id"],
                origin=origin,
                payload_hash=row["payload_hash"],
                previous_revision_hash=row["previous_revision_hash"],
                revision_hash=row["revision_hash"],
                recorded_at=row["recorded_at"],
                title=row["title"],
                content_json=row["content_json"],
                labels=labels,
                version=row["format_version"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, ProjectMemoryError):
                raise
            raise ProjectMemoryIntegrityError(
                "stored project-memory record is malformed"
            ) from exc
