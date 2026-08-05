"""Read-only, cryptographically verifiable index of persisted orchestration state.

The index scans one persistence root, ignores ELMAN-OS control directories,
verifies every candidate state directory, reconstructs valid orchestration
state through the restoration boundary, and classifies candidates as valid,
altered, or unreadable.

The component is read-only. It never creates, modifies, replaces, renames, or
removes persisted files. It never executes persisted content, imports
persisted code, performs network access, or invokes an AI provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_orchestration_state_persistence import (
    MANIFEST_FILE_NAME,
    ArtifactOrchestrationPersistenceError,
    ArtifactOrchestrationStateManifest,
)
from .artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationIntegrityError,
    ArtifactOrchestrationRestorationNotFoundError,
    ArtifactOrchestrationRestorationPolicy,
    ArtifactOrchestrationRestorationReadError,
    ArtifactOrchestrationRestorationRequest,
    ArtifactOrchestrationStateRestoration,
)
from .execution_checkpoint import ResumeAssessmentStatus


ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION: Final[int] = 1

_CONTROL_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".locks", ".staging"}
)
_STORAGE_KEY = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


class ArtifactOrchestrationStateIndexError(RuntimeError):
    """An index contract or operation is invalid."""


class ArtifactOrchestrationStateIndexNotFoundError(
    ArtifactOrchestrationStateIndexError
):
    """The requested persistence root does not exist."""


class ArtifactOrchestrationStateIndexIntegrityError(
    ArtifactOrchestrationStateIndexError
):
    """The persistence root or an index contract fails integrity checks."""


class ArtifactOrchestrationStateIndexReadError(
    ArtifactOrchestrationStateIndexError
):
    """The persistence root cannot be enumerated or inspected."""


class ArtifactOrchestrationStateIndexEntryStatus(StrEnum):
    VALID = "valid"
    ALTERED = "altered"
    UNREADABLE = "unreadable"


class ArtifactOrchestrationStateIndexStatus(StrEnum):
    INDEXED = "indexed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} has an invalid format"
        )
    return result


def _entry_name(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) > 255:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} exceeds 255 characters"
        )
    if result in {".", ".."} or "/" in result or "\\" in result:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be one path component"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationStateIndexError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationStateIndexError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationStateIndexError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationStateIndexError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationStateIndexError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _absolute_path(value: object, name: str) -> str:
    try:
        raw_value = os.fspath(value)
    except TypeError as exc:
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be a string or path-like value"
        ) from exc

    raw = _text(raw_value, name)
    path = Path(raw)
    if not path.is_absolute():
        raise ArtifactOrchestrationStateIndexError(
            f"{name} must be absolute"
        )
    return path.as_posix()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_document(data: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json(data).encode("utf-8"))


def _reject_symlink_components(path: Path) -> None:
    chain = tuple(reversed(path.parents)) + (path,)
    for component in chain:
        try:
            if component.exists() and component.is_symlink():
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    f"symlink path component is forbidden: {component}"
                )
        except OSError as exc:
            raise ArtifactOrchestrationStateIndexReadError(
                f"cannot inspect path component: {component}"
            ) from exc


def _read_regular_file(path: Path, *, max_file_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactOrchestrationStateIndexIntegrityError(
            f"required file is missing: {path.name}"
        ) from exc
    except PermissionError as exc:
        raise ArtifactOrchestrationStateIndexReadError(
            f"permission denied while inspecting: {path.name}"
        ) from exc
    except OSError as exc:
        raise ArtifactOrchestrationStateIndexReadError(
            f"cannot inspect file: {path.name}"
        ) from exc

    if stat.S_ISLNK(before.st_mode):
        raise ArtifactOrchestrationStateIndexIntegrityError(
            f"symlink file is forbidden: {path.name}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactOrchestrationStateIndexIntegrityError(
            f"entry is not a regular file: {path.name}"
        )
    if before.st_size > max_file_bytes:
        raise ArtifactOrchestrationStateIndexReadError(
            f"file exceeds max_file_bytes: {path.name}"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ArtifactOrchestrationStateIndexIntegrityError(
                f"opened entry is not a regular file: {path.name}"
            )
        if opened.st_size > max_file_bytes:
            raise ArtifactOrchestrationStateIndexReadError(
                f"file exceeds max_file_bytes: {path.name}"
            )
        if (
            before.st_dev != opened.st_dev
            or before.st_ino != opened.st_ino
        ):
            raise ArtifactOrchestrationStateIndexIntegrityError(
                f"file changed during open: {path.name}"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_file_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_file_bytes:
                raise ArtifactOrchestrationStateIndexReadError(
                    f"file exceeds max_file_bytes: {path.name}"
                )
            chunks.append(chunk)
        payload = b"".join(chunks)
    except ArtifactOrchestrationStateIndexError:
        raise
    except PermissionError as exc:
        raise ArtifactOrchestrationStateIndexReadError(
            f"permission denied while reading: {path.name}"
        ) from exc
    except OSError as exc:
        raise ArtifactOrchestrationStateIndexReadError(
            f"cannot read file: {path.name}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        after = path.lstat()
    except OSError as exc:
        raise ArtifactOrchestrationStateIndexReadError(
            f"cannot re-inspect file: {path.name}"
        ) from exc

    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ArtifactOrchestrationStateIndexIntegrityError(
            f"file changed during read: {path.name}"
        )

    return payload


def _decode_utf8(payload: bytes, name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactOrchestrationStateIndexIntegrityError(
            f"{name} is not valid UTF-8"
        ) from exc


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateIndexPolicy:
    policy_id: str
    reject_symlink_components: bool = True
    require_exact_entry_set: bool = True
    require_canonical_payloads: bool = True
    require_compatible_checkpoint: bool = True
    max_candidates: int = 10_000
    max_file_bytes: int = 64 * 1024 * 1024
    version: int = ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "reject_symlink_components",
            "require_exact_entry_set",
            "require_canonical_payloads",
            "require_compatible_checkpoint",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "max_candidates",
            _positive_int(self.max_candidates, "max_candidates"),
        )
        object.__setattr__(
            self,
            "max_file_bytes",
            _positive_int(self.max_file_bytes, "max_file_bytes"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION:
            raise ArtifactOrchestrationStateIndexError(
                "unsupported state index format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_index_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "reject_symlink_components": self.reject_symlink_components,
            "require_exact_entry_set": self.require_exact_entry_set,
            "require_canonical_payloads": self.require_canonical_payloads,
            "require_compatible_checkpoint": (
                self.require_compatible_checkpoint
            ),
            "max_candidates": self.max_candidates,
            "max_file_bytes": self.max_file_bytes,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())

    def to_restoration_policy(
        self,
    ) -> ArtifactOrchestrationRestorationPolicy:
        return ArtifactOrchestrationRestorationPolicy(
            policy_id=f"restoration-policy:{self.policy_hash}",
            reject_symlink_components=self.reject_symlink_components,
            require_exact_entry_set=self.require_exact_entry_set,
            require_canonical_payloads=self.require_canonical_payloads,
            require_compatible_checkpoint=(
                self.require_compatible_checkpoint
            ),
            max_file_bytes=self.max_file_bytes,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateIndexPolicy":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_index_policy"
        ):
            raise ArtifactOrchestrationStateIndexError(
                "record_type must be artifact_orchestration_state_index_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            reject_symlink_components=data[
                "reject_symlink_components"
            ],
            require_exact_entry_set=data["require_exact_entry_set"],
            require_canonical_payloads=data[
                "require_canonical_payloads"
            ],
            require_compatible_checkpoint=data[
                "require_compatible_checkpoint"
            ],
            max_candidates=data["max_candidates"],
            max_file_bytes=data["max_file_bytes"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateIndexPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateIndexError(
                "state index policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateIndexError(
                "state index policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateIndexEntry:
    storage_key: str
    status: ArtifactOrchestrationStateIndexEntryStatus
    state_directory: str
    reason_code: str
    reason: str
    persistence_id: str | None = None
    manifest_hash: str | None = None
    orchestration_result_hash: str | None = None
    plan_id: str | None = None
    project_id: str | None = None
    checkpoint_id: str | None = None
    assessment_status: ResumeAssessmentStatus | None = None
    can_resume: bool | None = None
    persisted_at: str | None = None
    state_hash: str | None = None
    entry_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "storage_key",
            _entry_name(self.storage_key, "storage_key"),
        )
        try:
            status = ArtifactOrchestrationStateIndexEntryStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateIndexError(
                "entry status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "state_directory",
            _absolute_path(self.state_directory, "state_directory"),
        )
        object.__setattr__(
            self,
            "reason_code",
            _identifier(
                self.reason_code,
                "reason_code",
                _REASON_CODE,
            ),
        )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        object.__setattr__(
            self,
            "persistence_id",
            (
                None
                if self.persistence_id is None
                else _identifier(
                    self.persistence_id,
                    "persistence_id",
                )
            ),
        )
        for field_name in (
            "manifest_hash",
            "orchestration_result_hash",
            "state_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_hash(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        for field_name in (
            "plan_id",
            "project_id",
            "checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )

        if self.assessment_status is None:
            assessment_status = None
        else:
            try:
                assessment_status = ResumeAssessmentStatus(
                    self.assessment_status
                )
            except (TypeError, ValueError) as exc:
                raise ArtifactOrchestrationStateIndexError(
                    "assessment_status is invalid"
                ) from exc
        object.__setattr__(
            self,
            "assessment_status",
            assessment_status,
        )

        if self.can_resume is not None and not isinstance(
            self.can_resume,
            bool,
        ):
            raise ArtifactOrchestrationStateIndexError(
                "can_resume must be boolean or null"
            )
        object.__setattr__(
            self,
            "persisted_at",
            (
                None
                if self.persisted_at is None
                else _utc_timestamp(
                    self.persisted_at,
                    "persisted_at",
                )
            ),
        )

        if status is ArtifactOrchestrationStateIndexEntryStatus.VALID:
            required = {
                "persistence_id": self.persistence_id,
                "manifest_hash": self.manifest_hash,
                "orchestration_result_hash": (
                    self.orchestration_result_hash
                ),
                "plan_id": self.plan_id,
                "project_id": self.project_id,
                "checkpoint_id": self.checkpoint_id,
                "assessment_status": self.assessment_status,
                "can_resume": self.can_resume,
                "persisted_at": self.persisted_at,
                "state_hash": self.state_hash,
            }
            missing = [
                name for name, value in required.items()
                if value is None
            ]
            if missing:
                raise ArtifactOrchestrationStateIndexError(
                    "valid entry is missing: " + ", ".join(missing)
                )
            expected_key = _sha256_bytes(
                self.persistence_id.encode("utf-8")
            )
            if self.storage_key != expected_key:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "valid entry storage_key does not match persistence_id"
                )
        else:
            if (
                self.assessment_status is not None
                or self.can_resume is not None
                or self.state_hash is not None
            ):
                raise ArtifactOrchestrationStateIndexError(
                    "non-valid entry cannot expose restored state fields"
                )

        if self.version != ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION:
            raise ArtifactOrchestrationStateIndexError(
                "unsupported state index format version"
            )

        computed = self.compute_hash()
        if self.entry_hash is None:
            object.__setattr__(self, "entry_hash", computed)
        else:
            supplied = _hash(self.entry_hash, "entry_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "entry hash does not match entry content"
                )
            object.__setattr__(self, "entry_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_index_entry",
            "version": self.version,
            "storage_key": self.storage_key,
            "status": self.status.value,
            "state_directory": self.state_directory,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "persistence_id": self.persistence_id,
            "manifest_hash": self.manifest_hash,
            "orchestration_result_hash": (
                self.orchestration_result_hash
            ),
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "checkpoint_id": self.checkpoint_id,
            "assessment_status": (
                None
                if self.assessment_status is None
                else self.assessment_status.value
            ),
            "can_resume": self.can_resume,
            "persisted_at": self.persisted_at,
            "state_hash": self.state_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.entry_hash != self.compute_hash():
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "entry hash does not match entry content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["entry_hash"] = self.entry_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateIndexEntry":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_index_entry"
        ):
            raise ArtifactOrchestrationStateIndexError(
                "record_type must be artifact_orchestration_state_index_entry"
            )
        if "entry_hash" not in data:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "serialized entry is missing entry_hash"
            )
        assessment = data.get("assessment_status")
        return cls(
            storage_key=data["storage_key"],
            status=ArtifactOrchestrationStateIndexEntryStatus(
                data["status"]
            ),
            state_directory=data["state_directory"],
            reason_code=data["reason_code"],
            reason=data["reason"],
            persistence_id=data.get("persistence_id"),
            manifest_hash=data.get("manifest_hash"),
            orchestration_result_hash=data.get(
                "orchestration_result_hash"
            ),
            plan_id=data.get("plan_id"),
            project_id=data.get("project_id"),
            checkpoint_id=data.get("checkpoint_id"),
            assessment_status=(
                None
                if assessment is None
                else ResumeAssessmentStatus(assessment)
            ),
            can_resume=data.get("can_resume"),
            persisted_at=data.get("persisted_at"),
            state_hash=data.get("state_hash"),
            entry_hash=data["entry_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateIndexEntry":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateIndexError(
                "state index entry JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateIndexError(
                "state index entry JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateIndexSnapshot:
    index_id: str
    policy_id: str
    policy_hash: str
    state_root: str
    requested_by: str
    indexed_at: str
    entries: tuple[ArtifactOrchestrationStateIndexEntry, ...]
    ignored_control_entries: tuple[str, ...] = ()
    valid_count: int = 0
    altered_count: int = 0
    unreadable_count: int = 0
    snapshot_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in ("index_id", "policy_id"):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        object.__setattr__(
            self,
            "state_root",
            _absolute_path(self.state_root, "state_root"),
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "indexed_at",
            _utc_timestamp(self.indexed_at, "indexed_at"),
        )

        entries = tuple(self.entries)
        if not all(
            isinstance(item, ArtifactOrchestrationStateIndexEntry)
            for item in entries
        ):
            raise ArtifactOrchestrationStateIndexError(
                "entries must contain state index entries"
            )
        for entry in entries:
            entry.verify_hash()
            try:
                Path(entry.state_directory).relative_to(
                    Path(self.state_root)
                )
            except ValueError as exc:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "entry state_directory escapes state_root"
                ) from exc

        sorted_entries = tuple(
            sorted(entries, key=lambda item: item.storage_key)
        )
        if sorted_entries != entries:
            raise ArtifactOrchestrationStateIndexError(
                "entries must be sorted by storage_key"
            )
        if len({entry.storage_key for entry in entries}) != len(entries):
            raise ArtifactOrchestrationStateIndexError(
                "entries contain duplicate storage_key values"
            )
        object.__setattr__(self, "entries", entries)

        controls = tuple(
            sorted(
                {
                    _entry_name(item, "ignored_control_entry")
                    for item in self.ignored_control_entries
                }
            )
        )
        if any(item not in _CONTROL_DIRECTORIES for item in controls):
            raise ArtifactOrchestrationStateIndexError(
                "ignored_control_entries contains an unknown control name"
            )
        object.__setattr__(
            self,
            "ignored_control_entries",
            controls,
        )

        expected_counts = {
            "valid_count": sum(
                entry.status
                is ArtifactOrchestrationStateIndexEntryStatus.VALID
                for entry in entries
            ),
            "altered_count": sum(
                entry.status
                is ArtifactOrchestrationStateIndexEntryStatus.ALTERED
                for entry in entries
            ),
            "unreadable_count": sum(
                entry.status
                is ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
                for entry in entries
            ),
        }
        for field_name, expected in expected_counts.items():
            supplied = _non_negative_int(
                getattr(self, field_name),
                field_name,
            )
            if supplied != expected:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    f"{field_name} does not match entries"
                )
            object.__setattr__(self, field_name, supplied)

        if self.version != ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION:
            raise ArtifactOrchestrationStateIndexError(
                "unsupported state index format version"
            )

        computed = self.compute_hash()
        if self.snapshot_hash is None:
            object.__setattr__(self, "snapshot_hash", computed)
        else:
            supplied = _hash(self.snapshot_hash, "snapshot_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "snapshot hash does not match snapshot content"
                )
            object.__setattr__(self, "snapshot_hash", supplied)

    @property
    def total_count(self) -> int:
        return len(self.entries)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_index_snapshot",
            "version": self.version,
            "index_id": self.index_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "indexed_at": self.indexed_at,
            "entries": [entry.to_dict() for entry in self.entries],
            "ignored_control_entries": list(
                self.ignored_control_entries
            ),
            "valid_count": self.valid_count,
            "altered_count": self.altered_count,
            "unreadable_count": self.unreadable_count,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.snapshot_hash != self.compute_hash():
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "snapshot hash does not match snapshot content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["snapshot_hash"] = self.snapshot_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateIndexSnapshot":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_index_snapshot"
        ):
            raise ArtifactOrchestrationStateIndexError(
                "record_type must be artifact_orchestration_state_index_snapshot"
            )
        if "snapshot_hash" not in data:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "serialized snapshot is missing snapshot_hash"
            )
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            raise ArtifactOrchestrationStateIndexError(
                "snapshot entries must be a list"
            )
        raw_controls = data.get("ignored_control_entries", [])
        if not isinstance(raw_controls, list):
            raise ArtifactOrchestrationStateIndexError(
                "ignored_control_entries must be a list"
            )
        return cls(
            index_id=data["index_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            state_root=data["state_root"],
            requested_by=data["requested_by"],
            indexed_at=data["indexed_at"],
            entries=tuple(
                ArtifactOrchestrationStateIndexEntry.from_dict(item)
                for item in raw_entries
            ),
            ignored_control_entries=tuple(raw_controls),
            valid_count=data["valid_count"],
            altered_count=data["altered_count"],
            unreadable_count=data["unreadable_count"],
            snapshot_hash=data["snapshot_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateIndexSnapshot":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateIndexError(
                "state index snapshot JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateIndexError(
                "state index snapshot JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateIndexResult:
    status: ArtifactOrchestrationStateIndexStatus
    index_id: str
    policy_id: str
    policy_hash: str
    state_root: str
    snapshot_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION

    def __post_init__(self) -> None:
        try:
            status = ArtifactOrchestrationStateIndexStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateIndexError(
                "state index result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        for field_name in ("index_id", "policy_id"):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        object.__setattr__(
            self,
            "state_root",
            _absolute_path(self.state_root, "state_root"),
        )

        snapshot_json = _text(self.snapshot_json, "snapshot_json")
        snapshot = ArtifactOrchestrationStateIndexSnapshot.from_json(
            snapshot_json
        )
        snapshot.verify_hash()
        if snapshot.index_id != self.index_id:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "snapshot index_id does not match result"
            )
        if snapshot.policy_id != self.policy_id:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "snapshot policy_id does not match result"
            )
        if snapshot.policy_hash != self.policy_hash:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "snapshot policy_hash does not match result"
            )
        if snapshot.state_root != self.state_root:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "snapshot state_root does not match result"
            )
        object.__setattr__(self, "snapshot_json", snapshot.to_json())
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

        if self.version != ARTIFACT_ORCHESTRATION_STATE_INDEX_FORMAT_VERSION:
            raise ArtifactOrchestrationStateIndexError(
                "unsupported state index format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def snapshot(self) -> ArtifactOrchestrationStateIndexSnapshot:
        return ArtifactOrchestrationStateIndexSnapshot.from_json(
            self.snapshot_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_index_result",
            "version": self.version,
            "status": self.status.value,
            "index_id": self.index_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "state_root": self.state_root,
            "snapshot_json": self.snapshot_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationStateIndexIntegrityError(
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
    ) -> "ArtifactOrchestrationStateIndexResult":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_index_result"
        ):
            raise ArtifactOrchestrationStateIndexError(
                "record_type must be artifact_orchestration_state_index_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            status=ArtifactOrchestrationStateIndexStatus(data["status"]),
            index_id=data["index_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            state_root=data["state_root"],
            snapshot_json=data["snapshot_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateIndexResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateIndexError(
                "state index result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateIndexError(
                "state index result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateIndex:
    policy: ArtifactOrchestrationStateIndexPolicy
    state_root: str
    requested_by: str
    indexed_at: str
    index_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.policy,
            ArtifactOrchestrationStateIndexPolicy,
        ):
            raise ArtifactOrchestrationStateIndexError(
                "policy must be an ArtifactOrchestrationStateIndexPolicy"
            )
        root = _absolute_path(self.state_root, "state_root")
        requester = _identifier(
            self.requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(self.indexed_at, "indexed_at")
        object.__setattr__(self, "state_root", root)
        object.__setattr__(self, "requested_by", requester)
        object.__setattr__(self, "indexed_at", timestamp)

        identity_hash = _sha256_document(
            {
                "record_type": "artifact_orchestration_state_index_identity",
                "policy_hash": self.policy.policy_hash,
                "state_root": root,
                "requested_by": requester,
                "indexed_at": timestamp,
            }
        )
        effective_id = (
            self.index_id
            if self.index_id is not None
            else f"orchestration-state-index:{identity_hash}"
        )
        object.__setattr__(
            self,
            "index_id",
            _identifier(effective_id, "index_id"),
        )

    def build(self) -> ArtifactOrchestrationStateIndexResult:
        root = Path(self.state_root)

        if self.policy.reject_symlink_components:
            _reject_symlink_components(root)

        if not root.exists():
            raise ArtifactOrchestrationStateIndexNotFoundError(
                "state_root does not exist"
            )
        if not root.is_dir() or root.is_symlink():
            raise ArtifactOrchestrationStateIndexIntegrityError(
                "state_root is not a regular directory"
            )

        candidates, controls = self._enumerate_root(root)
        entries = tuple(
            self._inspect_candidate(name, path)
            for name, path in candidates
        )

        valid_count = sum(
            entry.status
            is ArtifactOrchestrationStateIndexEntryStatus.VALID
            for entry in entries
        )
        altered_count = sum(
            entry.status
            is ArtifactOrchestrationStateIndexEntryStatus.ALTERED
            for entry in entries
        )
        unreadable_count = sum(
            entry.status
            is ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
            for entry in entries
        )

        snapshot = ArtifactOrchestrationStateIndexSnapshot(
            index_id=self.index_id,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            state_root=self.state_root,
            requested_by=self.requested_by,
            indexed_at=self.indexed_at,
            entries=entries,
            ignored_control_entries=controls,
            valid_count=valid_count,
            altered_count=altered_count,
            unreadable_count=unreadable_count,
        )

        return ArtifactOrchestrationStateIndexResult(
            status=ArtifactOrchestrationStateIndexStatus.INDEXED,
            index_id=self.index_id,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            state_root=self.state_root,
            snapshot_json=snapshot.to_json(),
            completed_at=self.indexed_at,
            reason=(
                "INDEXED: persistence root was scanned read-only and "
                f"classified {valid_count} valid, {altered_count} altered, "
                f"and {unreadable_count} unreadable state entries"
            ),
        )

    def _enumerate_root(
        self,
        root: Path,
    ) -> tuple[tuple[tuple[str, Path], ...], tuple[str, ...]]:
        candidates: list[tuple[str, Path]] = []
        controls: list[str] = []

        try:
            with os.scandir(root) as iterator:
                for raw_entry in iterator:
                    name = raw_entry.name
                    if name in _CONTROL_DIRECTORIES:
                        controls.append(name)
                        continue
                    candidates.append((name, Path(raw_entry.path)))
                    if len(candidates) > self.policy.max_candidates:
                        raise ArtifactOrchestrationStateIndexReadError(
                            "state_root exceeds policy max_candidates"
                        )
        except ArtifactOrchestrationStateIndexError:
            raise
        except PermissionError as exc:
            raise ArtifactOrchestrationStateIndexReadError(
                "permission denied while enumerating state_root"
            ) from exc
        except OSError as exc:
            raise ArtifactOrchestrationStateIndexReadError(
                "cannot enumerate state_root"
            ) from exc

        candidates.sort(key=lambda item: item[0])
        controls.sort()
        return tuple(candidates), tuple(controls)

    def _inspect_candidate(
        self,
        storage_key: str,
        path: Path,
    ) -> ArtifactOrchestrationStateIndexEntry:
        try:
            normalized_key = _entry_name(
                storage_key,
                "storage_key",
            )
        except ArtifactOrchestrationStateIndexError as exc:
            return self._diagnostic_entry(
                storage_key=str(storage_key),
                path=path,
                status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
                reason_code="invalid-storage-key",
                reason=str(exc),
            )

        if _STORAGE_KEY.fullmatch(normalized_key) is None:
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
                reason_code="invalid-storage-key",
                reason=(
                    "ALTERED: state directory name must be a lowercase "
                    "SHA-256 storage key"
                ),
            )

        try:
            metadata = path.lstat()
        except PermissionError as exc:
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=(
                    ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
                ),
                reason_code="entry-permission-denied",
                reason=f"UNREADABLE: {exc}",
            )
        except OSError as exc:
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=(
                    ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
                ),
                reason_code="entry-inspection-failed",
                reason=f"UNREADABLE: {exc}",
            )

        if stat.S_ISLNK(metadata.st_mode):
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
                reason_code="symlink-state-entry",
                reason="ALTERED: state root entry is a symlink",
            )
        if not stat.S_ISDIR(metadata.st_mode):
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
                reason_code="non-directory-state-entry",
                reason="ALTERED: state root entry is not a directory",
            )

        try:
            if self.policy.reject_symlink_components:
                _reject_symlink_components(path)

            manifest_payload = _read_regular_file(
                path / MANIFEST_FILE_NAME,
                max_file_bytes=self.policy.max_file_bytes,
            )
            manifest_text = _decode_utf8(
                manifest_payload,
                MANIFEST_FILE_NAME,
            )
            manifest = ArtifactOrchestrationStateManifest.from_json(
                manifest_text
            )
            manifest.verify_hash()

            if (
                self.policy.require_canonical_payloads
                and manifest.to_json() != manifest_text
            ):
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "manifest is not canonical"
                )

            expected_key = _sha256_bytes(
                manifest.persistence_id.encode("utf-8")
            )
            if normalized_key != expected_key:
                raise ArtifactOrchestrationStateIndexIntegrityError(
                    "state directory name does not match manifest persistence_id"
                )

            restoration_policy = (
                self.policy.to_restoration_policy()
            )
            request = (
                ArtifactOrchestrationRestorationRequest.from_identifiers(
                    persistence_id=manifest.persistence_id,
                    state_root=self.state_root,
                    policy=restoration_policy,
                    requested_by=self.requested_by,
                    requested_at=self.indexed_at,
                    expected_manifest_hash=manifest.manifest_hash,
                    expected_orchestration_result_hash=(
                        manifest.orchestration_result_hash
                    ),
                )
            )
            restoration = ArtifactOrchestrationStateRestoration(
                request,
                restoration_policy,
            )
            restored = restoration.restore()
            restored_state = restored.restored_state
            restored_state.verify_hash()

            return ArtifactOrchestrationStateIndexEntry(
                storage_key=normalized_key,
                status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                state_directory=path.as_posix(),
                reason_code="verified-state",
                reason=(
                    "VALID: manifest, plan, journal, checkpoint, and "
                    "resume assessment were verified"
                ),
                persistence_id=manifest.persistence_id,
                manifest_hash=manifest.manifest_hash,
                orchestration_result_hash=(
                    manifest.orchestration_result_hash
                ),
                plan_id=restored_state.plan_id,
                project_id=restored_state.project_id,
                checkpoint_id=restored_state.checkpoint_id,
                assessment_status=restored_state.assessment_status,
                can_resume=restored_state.can_resume,
                persisted_at=manifest.persisted_at,
                state_hash=restored_state.state_hash,
            )

        except (
            ArtifactOrchestrationStateIndexIntegrityError,
            ArtifactOrchestrationRestorationIntegrityError,
            ArtifactOrchestrationRestorationNotFoundError,
            ArtifactOrchestrationPersistenceError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
                reason_code="state-integrity-failed",
                reason=f"ALTERED: {exc}",
            )
        except (
            ArtifactOrchestrationStateIndexReadError,
            ArtifactOrchestrationRestorationReadError,
            PermissionError,
            OSError,
        ) as exc:
            return self._diagnostic_entry(
                storage_key=normalized_key,
                path=path,
                status=(
                    ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
                ),
                reason_code="state-read-failed",
                reason=f"UNREADABLE: {exc}",
            )

    def _diagnostic_entry(
        self,
        *,
        storage_key: str,
        path: Path,
        status: ArtifactOrchestrationStateIndexEntryStatus,
        reason_code: str,
        reason: str,
    ) -> ArtifactOrchestrationStateIndexEntry:
        safe_key = str(storage_key)
        if (
            not safe_key
            or safe_key in {".", ".."}
            or "/" in safe_key
            or "\\" in safe_key
            or len(safe_key) > 255
        ):
            safe_key = "invalid-entry-" + _sha256_bytes(
                safe_key.encode("utf-8", errors="replace")
            )
        return ArtifactOrchestrationStateIndexEntry(
            storage_key=safe_key,
            status=status,
            state_directory=path.absolute().as_posix(),
            reason_code=reason_code,
            reason=reason,
        )
