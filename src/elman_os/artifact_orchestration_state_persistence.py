"""Durable, atomic persistence for ELMAN-OS orchestration state.

The persistence boundary stores the verified execution plan, append-only
journal, and execution checkpoint embedded in an
ArtifactTransactionOrchestrationResult. The three payloads are first written
and verified inside a private staging directory. A manifest cryptographically
binds the files to the orchestration result and request. The complete staging
directory is then renamed into its immutable final location.

The component never executes persisted content and never performs network,
provider, or AI calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_transaction_orchestration_adapter import (
    ArtifactTransactionOrchestrationResult,
)


ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION: Final[int] = 1

PLAN_FILE_NAME: Final[str] = "execution-plan.json"
JOURNAL_FILE_NAME: Final[str] = "execution-journal.jsonl"
CHECKPOINT_FILE_NAME: Final[str] = "execution-checkpoint.json"
MANIFEST_FILE_NAME: Final[str] = "manifest.json"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RELATIVE_FILE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,127}$")


class ArtifactOrchestrationPersistenceError(RuntimeError):
    """A persistence contract or operation is invalid."""


class ArtifactOrchestrationPersistenceIntegrityError(
    ArtifactOrchestrationPersistenceError
):
    """Persisted orchestration state fails integrity validation."""


class ArtifactOrchestrationPersistenceLockError(
    ArtifactOrchestrationPersistenceError
):
    """The exclusive persistence lock cannot be acquired or released."""


class ArtifactOrchestrationPersistenceStatus(StrEnum):
    PERSISTED = "persisted"
    NOOP = "noop"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationPersistenceError(
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
        raise ArtifactOrchestrationPersistenceError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationPersistenceError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationPersistenceError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationPersistenceError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationPersistenceError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationPersistenceError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_document(data: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        canonical_json(data).encode("utf-8")
    )


def _absolute_path(value: object, name: str) -> str:
    try:
        raw_value = os.fspath(value)
    except TypeError as exc:
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be a string or path-like value"
        ) from exc
    raw = _text(raw_value, name)
    path = Path(raw)
    if not path.is_absolute():
        raise ArtifactOrchestrationPersistenceError(
            f"{name} must be absolute"
        )
    return path.as_posix()


def _path_components(path: Path) -> tuple[Path, ...]:
    absolute = Path(path)
    anchor = Path(absolute.anchor)
    components: list[Path] = [anchor]
    current = anchor
    start = 1 if absolute.parts and absolute.parts[0] == absolute.anchor else 0
    for part in absolute.parts[start:]:
        current = current / part
        components.append(current)
    return tuple(components)


def _reject_symlink_components(path: Path) -> None:
    for component in _path_components(path):
        if component.exists() and component.is_symlink():
            raise ArtifactOrchestrationPersistenceIntegrityError(
                f"symlink path component is forbidden: {component}"
            )


def _ensure_directory(path: Path, *, reject_symlinks: bool) -> None:
    if reject_symlinks:
        _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ArtifactOrchestrationPersistenceIntegrityError(
            f"persistence directory is invalid: {path}"
        )


def _fsync_directory(
    path: Path,
    *,
    required: bool,
) -> None:
    if not required:
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactOrchestrationPersistenceError(
            f"cannot open directory for fsync: {path}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ArtifactOrchestrationPersistenceError(
            f"cannot fsync directory: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def _write_atomic(
    destination: Path,
    payload: bytes,
    *,
    fsync_file: bool,
) -> None:
    temporary = destination.with_name(
        destination.name + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            if fsync_file:
                os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise


def _safe_remove_tree(path: Path, *, expected_parent: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise ArtifactOrchestrationPersistenceIntegrityError(
            f"refusing to remove symlink staging path: {path}"
        )
    try:
        path.relative_to(expected_parent)
    except ValueError as exc:
        raise ArtifactOrchestrationPersistenceIntegrityError(
            "staging path escapes expected parent"
        ) from exc
    shutil.rmtree(path)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationPersistencePolicy:
    policy_id: str
    fsync_files: bool = True
    require_directory_fsync: bool = False
    reject_symlink_components: bool = True
    max_file_bytes: int = 64 * 1024 * 1024
    version: int = ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "fsync_files",
            "require_directory_fsync",
            "reject_symlink_components",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "max_file_bytes",
            _positive_int(self.max_file_bytes, "max_file_bytes"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION:
            raise ArtifactOrchestrationPersistenceError(
                "unsupported persistence format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_persistence_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "fsync_files": self.fsync_files,
            "require_directory_fsync": self.require_directory_fsync,
            "reject_symlink_components": self.reject_symlink_components,
            "max_file_bytes": self.max_file_bytes,
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
    ) -> "ArtifactOrchestrationPersistencePolicy":
        if (
            data.get("record_type")
            != "artifact_orchestration_persistence_policy"
        ):
            raise ArtifactOrchestrationPersistenceError(
                "record_type must be artifact_orchestration_persistence_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            fsync_files=data["fsync_files"],
            require_directory_fsync=data[
                "require_directory_fsync"
            ],
            reject_symlink_components=data[
                "reject_symlink_components"
            ],
            max_file_bytes=data["max_file_bytes"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationPersistencePolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationPersistenceError(
                "persistence policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationPersistenceError(
                "persistence policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationPersistenceRequest:
    persistence_id: str
    policy_id: str
    policy_hash: str
    orchestration_id: str
    orchestration_result_hash: str
    plan_id: str
    project_id: str
    step_id: str
    agent_id: str
    transaction_id: str
    result_plan_state_hash: str
    result_journal_hash: str
    result_checkpoint_hash: str
    state_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "persistence_id",
            "policy_id",
            "orchestration_id",
            "plan_id",
            "project_id",
            "transaction_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "policy_hash",
            "orchestration_result_hash",
            "result_plan_state_hash",
            "result_journal_hash",
            "result_checkpoint_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "step_id",
            _identifier(self.step_id, "step_id", _SAFE_RELATIVE_FILE),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
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
            "requested_at",
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION:
            raise ArtifactOrchestrationPersistenceError(
                "unsupported persistence format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        orchestration_result: ArtifactTransactionOrchestrationResult,
        policy: ArtifactOrchestrationPersistencePolicy,
        *,
        state_root: str | Path,
        requested_by: str,
        requested_at: str | datetime,
        persistence_id: str | None = None,
    ) -> "ArtifactOrchestrationPersistenceRequest":
        if not isinstance(
            orchestration_result,
            ArtifactTransactionOrchestrationResult,
        ):
            raise ArtifactOrchestrationPersistenceError(
                "orchestration_result must be an "
                "ArtifactTransactionOrchestrationResult"
            )
        if not isinstance(
            policy,
            ArtifactOrchestrationPersistencePolicy,
        ):
            raise ArtifactOrchestrationPersistenceError(
                "policy must be an ArtifactOrchestrationPersistencePolicy"
            )
        orchestration_result.verify_hash()
        result_hash = orchestration_result.result_hash
        assert result_hash is not None
        root = _absolute_path(state_root, "state_root")
        requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(requested_at, "requested_at")
        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_orchestration_persistence_identity"
                ),
                "policy_hash": policy.policy_hash,
                "orchestration_result_hash": result_hash,
                "state_root": root,
            }
        )
        effective_id = (
            persistence_id
            if persistence_id is not None
            else f"orchestration-persistence:{identity_hash}"
        )
        return cls(
            persistence_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            orchestration_id=orchestration_result.orchestration_id,
            orchestration_result_hash=result_hash,
            plan_id=orchestration_result.plan_id,
            project_id=orchestration_result.project_id,
            step_id=orchestration_result.step_id,
            agent_id=orchestration_result.agent_id,
            transaction_id=orchestration_result.transaction_id,
            result_plan_state_hash=(
                orchestration_result.result_plan_state_hash
            ),
            result_journal_hash=(
                orchestration_result.result_journal_hash
            ),
            result_checkpoint_hash=(
                orchestration_result.result_checkpoint_hash
            ),
            state_root=root,
            requested_by=requester,
            requested_at=timestamp,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_persistence_request",
            "version": self.version,
            "persistence_id": self.persistence_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "orchestration_id": self.orchestration_id,
            "orchestration_result_hash": (
                self.orchestration_result_hash
            ),
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "transaction_id": self.transaction_id,
            "result_plan_state_hash": self.result_plan_state_hash,
            "result_journal_hash": self.result_journal_hash,
            "result_checkpoint_hash": self.result_checkpoint_hash,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationPersistenceIntegrityError(
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
    ) -> "ArtifactOrchestrationPersistenceRequest":
        if (
            data.get("record_type")
            != "artifact_orchestration_persistence_request"
        ):
            raise ArtifactOrchestrationPersistenceError(
                "record_type must be artifact_orchestration_persistence_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            persistence_id=data["persistence_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            orchestration_id=data["orchestration_id"],
            orchestration_result_hash=data[
                "orchestration_result_hash"
            ],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            transaction_id=data["transaction_id"],
            result_plan_state_hash=data[
                "result_plan_state_hash"
            ],
            result_journal_hash=data["result_journal_hash"],
            result_checkpoint_hash=data[
                "result_checkpoint_hash"
            ],
            state_root=data["state_root"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationPersistenceRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationPersistenceError(
                "persistence request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationPersistenceError(
                "persistence request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationPersistenceFile:
    path: str
    media_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        normalized = _identifier(
            self.path,
            "path",
            _SAFE_RELATIVE_FILE,
        )
        if "/" in normalized or "\\" in normalized:
            raise ArtifactOrchestrationPersistenceError(
                "persistence file path must be a single file name"
            )
        object.__setattr__(self, "path", normalized)
        object.__setattr__(
            self,
            "media_type",
            _text(self.media_type, "media_type"),
        )
        object.__setattr__(
            self,
            "size_bytes",
            _non_negative_int(self.size_bytes, "size_bytes"),
        )
        object.__setattr__(
            self,
            "sha256",
            _hash(self.sha256, "sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationPersistenceFile":
        return cls(
            path=data["path"],
            media_type=data["media_type"],
            size_bytes=data["size_bytes"],
            sha256=data["sha256"],
        )


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateManifest:
    persistence_id: str
    request_hash: str
    policy_id: str
    policy_hash: str
    orchestration_id: str
    orchestration_result_hash: str
    plan_id: str
    project_id: str
    step_id: str
    agent_id: str
    transaction_id: str
    result_plan_state_hash: str
    result_journal_hash: str
    result_checkpoint_hash: str
    files: tuple[ArtifactOrchestrationPersistenceFile, ...]
    persisted_at: str
    manifest_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "persistence_id",
            "policy_id",
            "orchestration_id",
            "plan_id",
            "project_id",
            "transaction_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "request_hash",
            "policy_hash",
            "orchestration_result_hash",
            "result_plan_state_hash",
            "result_journal_hash",
            "result_checkpoint_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "step_id",
            _identifier(self.step_id, "step_id", _SAFE_RELATIVE_FILE),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        entries = tuple(self.files)
        if not all(
            isinstance(item, ArtifactOrchestrationPersistenceFile)
            for item in entries
        ):
            raise ArtifactOrchestrationPersistenceError(
                "files must contain persistence file entries"
            )
        entries = tuple(sorted(entries, key=lambda item: item.path))
        expected_names = {
            PLAN_FILE_NAME,
            JOURNAL_FILE_NAME,
            CHECKPOINT_FILE_NAME,
        }
        names = {item.path for item in entries}
        if names != expected_names or len(entries) != len(expected_names):
            raise ArtifactOrchestrationPersistenceError(
                "manifest must describe exactly the three orchestration files"
            )
        object.__setattr__(self, "files", entries)
        object.__setattr__(
            self,
            "persisted_at",
            _utc_timestamp(self.persisted_at, "persisted_at"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION:
            raise ArtifactOrchestrationPersistenceError(
                "unsupported persistence format version"
            )
        computed = self.compute_hash()
        if self.manifest_hash is None:
            object.__setattr__(self, "manifest_hash", computed)
        else:
            supplied = _hash(self.manifest_hash, "manifest_hash")
            if supplied != computed:
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    "manifest hash does not match manifest content"
                )
            object.__setattr__(self, "manifest_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_manifest",
            "version": self.version,
            "persistence_id": self.persistence_id,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "orchestration_id": self.orchestration_id,
            "orchestration_result_hash": (
                self.orchestration_result_hash
            ),
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "transaction_id": self.transaction_id,
            "result_plan_state_hash": self.result_plan_state_hash,
            "result_journal_hash": self.result_journal_hash,
            "result_checkpoint_hash": self.result_checkpoint_hash,
            "files": [item.to_dict() for item in self.files],
            "persisted_at": self.persisted_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.manifest_hash != self.compute_hash():
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest hash does not match manifest content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["manifest_hash"] = self.manifest_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateManifest":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_manifest"
        ):
            raise ArtifactOrchestrationPersistenceError(
                "record_type must be artifact_orchestration_state_manifest"
            )
        if "manifest_hash" not in data:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "serialized manifest is missing manifest_hash"
            )
        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            raise ArtifactOrchestrationPersistenceError(
                "manifest files must be a list"
            )
        return cls(
            persistence_id=data["persistence_id"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            orchestration_id=data["orchestration_id"],
            orchestration_result_hash=data[
                "orchestration_result_hash"
            ],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            transaction_id=data["transaction_id"],
            result_plan_state_hash=data[
                "result_plan_state_hash"
            ],
            result_journal_hash=data["result_journal_hash"],
            result_checkpoint_hash=data[
                "result_checkpoint_hash"
            ],
            files=tuple(
                ArtifactOrchestrationPersistenceFile.from_dict(item)
                for item in raw_files
            ),
            persisted_at=data["persisted_at"],
            manifest_hash=data["manifest_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateManifest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationPersistenceError(
                "manifest JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationPersistenceError(
                "manifest JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationPersistenceResult:
    persistence_id: str
    status: ArtifactOrchestrationPersistenceStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    orchestration_id: str
    orchestration_result_hash: str
    state_root: str
    state_directory: str
    manifest_hash: str
    manifest_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "persistence_id",
            "policy_id",
            "orchestration_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        try:
            status = ArtifactOrchestrationPersistenceStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationPersistenceError(
                "persistence status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        for field_name in (
            "request_hash",
            "policy_hash",
            "orchestration_result_hash",
            "manifest_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        root = _absolute_path(self.state_root, "state_root")
        directory = _absolute_path(
            self.state_directory,
            "state_directory",
        )
        try:
            Path(directory).relative_to(Path(root))
        except ValueError as exc:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "state_directory escapes state_root"
            ) from exc
        object.__setattr__(self, "state_root", root)
        object.__setattr__(self, "state_directory", directory)
        manifest_json = _text(self.manifest_json, "manifest_json")
        manifest = ArtifactOrchestrationStateManifest.from_json(
            manifest_json
        )
        manifest.verify_hash()
        if manifest.persistence_id != self.persistence_id:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest persistence_id does not match result"
            )
        if manifest.request_hash != self.request_hash:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest request_hash does not match result"
            )
        if manifest.policy_id != self.policy_id:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest policy_id does not match result"
            )
        if manifest.policy_hash != self.policy_hash:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest policy_hash does not match result"
            )
        if manifest.orchestration_id != self.orchestration_id:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest orchestration_id does not match result"
            )
        if (
            manifest.orchestration_result_hash
            != self.orchestration_result_hash
        ):
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest orchestration result hash does not match result"
            )
        if manifest.manifest_hash != self.manifest_hash:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "manifest hash does not match result"
            )
        object.__setattr__(self, "manifest_json", manifest.to_json())
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
        if self.version != ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION:
            raise ArtifactOrchestrationPersistenceError(
                "unsupported persistence format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def manifest(self) -> ArtifactOrchestrationStateManifest:
        return ArtifactOrchestrationStateManifest.from_json(
            self.manifest_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_persistence_result",
            "version": self.version,
            "persistence_id": self.persistence_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "orchestration_id": self.orchestration_id,
            "orchestration_result_hash": (
                self.orchestration_result_hash
            ),
            "state_root": self.state_root,
            "state_directory": self.state_directory,
            "manifest_hash": self.manifest_hash,
            "manifest_json": self.manifest_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationPersistenceIntegrityError(
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
    ) -> "ArtifactOrchestrationPersistenceResult":
        if (
            data.get("record_type")
            != "artifact_orchestration_persistence_result"
        ):
            raise ArtifactOrchestrationPersistenceError(
                "record_type must be artifact_orchestration_persistence_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            persistence_id=data["persistence_id"],
            status=ArtifactOrchestrationPersistenceStatus(
                data["status"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            orchestration_id=data["orchestration_id"],
            orchestration_result_hash=data[
                "orchestration_result_hash"
            ],
            state_root=data["state_root"],
            state_directory=data["state_directory"],
            manifest_hash=data["manifest_hash"],
            manifest_json=data["manifest_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationPersistenceResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationPersistenceError(
                "persistence result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationPersistenceError(
                "persistence result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStatePersistence:
    request: ArtifactOrchestrationPersistenceRequest
    orchestration_result: ArtifactTransactionOrchestrationResult
    policy: ArtifactOrchestrationPersistencePolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationPersistenceRequest,
        ):
            raise ArtifactOrchestrationPersistenceError(
                "request must be an ArtifactOrchestrationPersistenceRequest"
            )
        if not isinstance(
            self.orchestration_result,
            ArtifactTransactionOrchestrationResult,
        ):
            raise ArtifactOrchestrationPersistenceError(
                "orchestration_result must be an "
                "ArtifactTransactionOrchestrationResult"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationPersistencePolicy,
        ):
            raise ArtifactOrchestrationPersistenceError(
                "policy must be an ArtifactOrchestrationPersistencePolicy"
            )
        self.request.verify_hash()
        self.orchestration_result.verify_hash()
        result_hash = self.orchestration_result.result_hash
        assert result_hash is not None
        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "orchestration_id": (
                self.orchestration_result.orchestration_id
            ),
            "orchestration_result_hash": result_hash,
            "plan_id": self.orchestration_result.plan_id,
            "project_id": self.orchestration_result.project_id,
            "step_id": self.orchestration_result.step_id,
            "agent_id": self.orchestration_result.agent_id,
            "transaction_id": (
                self.orchestration_result.transaction_id
            ),
            "result_plan_state_hash": (
                self.orchestration_result.result_plan_state_hash
            ),
            "result_journal_hash": (
                self.orchestration_result.result_journal_hash
            ),
            "result_checkpoint_hash": (
                self.orchestration_result.result_checkpoint_hash
            ),
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactOrchestrationPersistenceError(
                    f"request {field_name} does not match persistence source"
                )

    @property
    def state_root(self) -> Path:
        return Path(self.request.state_root)

    @property
    def state_key(self) -> str:
        return _sha256_bytes(
            self.request.persistence_id.encode("utf-8")
        )

    @property
    def state_directory(self) -> Path:
        return self.state_root / self.state_key

    @property
    def staging_directory(self) -> Path:
        request_hash = self.request.request_hash
        assert request_hash is not None
        return (
            self.state_root
            / ".staging"
            / f"{self.state_key}.{request_hash[:16]}"
        )

    @property
    def lock_path(self) -> Path:
        return (
            self.state_root
            / ".locks"
            / f"{self.state_key}.lock"
        )

    def persist(self) -> ArtifactOrchestrationPersistenceResult:
        payloads = self._payloads()
        manifest = self._manifest(payloads)
        root = self.state_root
        if self.policy.reject_symlink_components:
            _reject_symlink_components(root)
        _ensure_directory(
            root,
            reject_symlinks=self.policy.reject_symlink_components,
        )
        _ensure_directory(
            self.lock_path.parent,
            reject_symlinks=self.policy.reject_symlink_components,
        )
        _ensure_directory(
            self.staging_directory.parent,
            reject_symlinks=self.policy.reject_symlink_components,
        )
        lock_payload = self._acquire_lock()
        created_staging = False
        committed = False
        try:
            if self.state_directory.exists():
                self._verify_state_directory(
                    self.state_directory,
                    payloads,
                    manifest,
                )
                return self._result(
                    ArtifactOrchestrationPersistenceStatus.NOOP,
                    manifest,
                    reason=(
                        "NOOP: identical orchestration state is already "
                        "persisted"
                    ),
                )

            if self.staging_directory.exists():
                self._verify_state_directory(
                    self.staging_directory,
                    payloads,
                    manifest,
                )
            else:
                self.staging_directory.mkdir(
                    parents=False,
                    exist_ok=False,
                )
                created_staging = True
                self._write_staging(payloads, manifest)

            if self.state_directory.exists():
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    "final state directory appeared during persistence"
                )
            os.rename(
                self.staging_directory,
                self.state_directory,
            )
            committed = True
            _fsync_directory(
                root,
                required=self.policy.require_directory_fsync,
            )
            self._verify_state_directory(
                self.state_directory,
                payloads,
                manifest,
            )
            return self._result(
                ArtifactOrchestrationPersistenceStatus.PERSISTED,
                manifest,
                reason=(
                    "PERSISTED: verified orchestration plan, journal, "
                    "checkpoint, and manifest were committed atomically"
                ),
            )
        except Exception:
            if (
                created_staging
                and not committed
                and self.staging_directory.exists()
            ):
                _safe_remove_tree(
                    self.staging_directory,
                    expected_parent=self.staging_directory.parent,
                )
            raise
        finally:
            self._release_lock(lock_payload)

    def _payloads(self) -> dict[str, bytes]:
        payloads = {
            PLAN_FILE_NAME: (
                self.orchestration_result.updated_plan_json
                .encode("utf-8")
            ),
            JOURNAL_FILE_NAME: (
                self.orchestration_result.updated_journal_jsonl
                .encode("utf-8")
            ),
            CHECKPOINT_FILE_NAME: (
                self.orchestration_result.updated_checkpoint_json
                .encode("utf-8")
            ),
        }
        for name, payload in payloads.items():
            if len(payload) > self.policy.max_file_bytes:
                raise ArtifactOrchestrationPersistenceError(
                    f"{name} exceeds policy max_file_bytes"
                )
        return payloads

    def _manifest(
        self,
        payloads: Mapping[str, bytes],
    ) -> ArtifactOrchestrationStateManifest:
        request_hash = self.request.request_hash
        result_hash = self.orchestration_result.result_hash
        assert request_hash is not None
        assert result_hash is not None
        media_types = {
            PLAN_FILE_NAME: "application/json",
            JOURNAL_FILE_NAME: "application/x-ndjson",
            CHECKPOINT_FILE_NAME: "application/json",
        }
        entries = tuple(
            ArtifactOrchestrationPersistenceFile(
                path=name,
                media_type=media_types[name],
                size_bytes=len(payloads[name]),
                sha256=_sha256_bytes(payloads[name]),
            )
            for name in sorted(payloads)
        )
        return ArtifactOrchestrationStateManifest(
            persistence_id=self.request.persistence_id,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            orchestration_id=(
                self.orchestration_result.orchestration_id
            ),
            orchestration_result_hash=result_hash,
            plan_id=self.orchestration_result.plan_id,
            project_id=self.orchestration_result.project_id,
            step_id=self.orchestration_result.step_id,
            agent_id=self.orchestration_result.agent_id,
            transaction_id=(
                self.orchestration_result.transaction_id
            ),
            result_plan_state_hash=(
                self.orchestration_result.result_plan_state_hash
            ),
            result_journal_hash=(
                self.orchestration_result.result_journal_hash
            ),
            result_checkpoint_hash=(
                self.orchestration_result.result_checkpoint_hash
            ),
            files=entries,
            persisted_at=self.request.requested_at,
        )

    def _write_staging(
        self,
        payloads: Mapping[str, bytes],
        manifest: ArtifactOrchestrationStateManifest,
    ) -> None:
        for name in (
            PLAN_FILE_NAME,
            JOURNAL_FILE_NAME,
            CHECKPOINT_FILE_NAME,
        ):
            _write_atomic(
                self.staging_directory / name,
                payloads[name],
                fsync_file=self.policy.fsync_files,
            )
        _write_atomic(
            self.staging_directory / MANIFEST_FILE_NAME,
            manifest.to_json().encode("utf-8"),
            fsync_file=self.policy.fsync_files,
        )
        _fsync_directory(
            self.staging_directory,
            required=self.policy.require_directory_fsync,
        )
        self._verify_state_directory(
            self.staging_directory,
            payloads,
            manifest,
        )

    def _verify_state_directory(
        self,
        directory: Path,
        payloads: Mapping[str, bytes],
        manifest: ArtifactOrchestrationStateManifest,
    ) -> None:
        if not directory.is_dir() or directory.is_symlink():
            raise ArtifactOrchestrationPersistenceIntegrityError(
                f"state directory is invalid: {directory}"
            )
        expected_names = {
            PLAN_FILE_NAME,
            JOURNAL_FILE_NAME,
            CHECKPOINT_FILE_NAME,
            MANIFEST_FILE_NAME,
        }
        actual_names = {
            item.name
            for item in directory.iterdir()
        }
        if actual_names != expected_names:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "state directory contains missing or unexpected entries"
            )
        for name, expected_payload in payloads.items():
            path = directory / name
            if not path.is_file() or path.is_symlink():
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    f"persisted state file is invalid: {name}"
                )
            observed = path.read_bytes()
            if observed != expected_payload:
                raise ArtifactOrchestrationPersistenceIntegrityError(
                    f"persisted state file differs: {name}"
                )
        manifest_path = directory / MANIFEST_FILE_NAME
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
        ):
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "persisted manifest is invalid"
            )
        try:
            observed_manifest = (
                ArtifactOrchestrationStateManifest.from_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            )
        except (
            UnicodeDecodeError,
            ArtifactOrchestrationPersistenceError,
        ) as exc:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "persisted manifest cannot be decoded or verified"
            ) from exc
        observed_manifest.verify_hash()
        if observed_manifest != manifest:
            raise ArtifactOrchestrationPersistenceIntegrityError(
                "persisted manifest differs from expected manifest"
            )

    def _acquire_lock(self) -> bytes:
        request_hash = self.request.request_hash
        assert request_hash is not None
        document = canonical_json(
            {
                "record_type": "artifact_orchestration_persistence_lock",
                "persistence_id": self.request.persistence_id,
                "request_hash": request_hash,
            }
        ).encode("utf-8")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                if self.policy.fsync_files:
                    os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ArtifactOrchestrationPersistenceLockError(
                "persistence lock already exists"
            ) from exc
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            if self.lock_path.exists() and not self.lock_path.is_symlink():
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
            raise
        return document

    def _release_lock(self, expected_payload: bytes) -> None:
        if not self.lock_path.exists():
            raise ArtifactOrchestrationPersistenceLockError(
                "persistence lock disappeared before release"
            )
        if not self.lock_path.is_file() or self.lock_path.is_symlink():
            raise ArtifactOrchestrationPersistenceLockError(
                "persistence lock changed type before release"
            )
        observed = self.lock_path.read_bytes()
        if observed != expected_payload:
            raise ArtifactOrchestrationPersistenceLockError(
                "persistence lock content changed before release"
            )
        self.lock_path.unlink()
        _fsync_directory(
            self.lock_path.parent,
            required=self.policy.require_directory_fsync,
        )

    def _result(
        self,
        status: ArtifactOrchestrationPersistenceStatus,
        manifest: ArtifactOrchestrationStateManifest,
        *,
        reason: str,
    ) -> ArtifactOrchestrationPersistenceResult:
        request_hash = self.request.request_hash
        result_hash = self.orchestration_result.result_hash
        manifest_hash = manifest.manifest_hash
        assert request_hash is not None
        assert result_hash is not None
        assert manifest_hash is not None
        return ArtifactOrchestrationPersistenceResult(
            persistence_id=self.request.persistence_id,
            status=status,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            orchestration_id=(
                self.orchestration_result.orchestration_id
            ),
            orchestration_result_hash=result_hash,
            state_root=self.state_root.as_posix(),
            state_directory=self.state_directory.as_posix(),
            manifest_hash=manifest_hash,
            manifest_json=manifest.to_json(),
            completed_at=self.request.requested_at,
            reason=reason,
        )
