"""Atomic persistence of an applied selected-state resume for ELMAN-OS v0.7.

This boundary consumes a verified selected-state resume application result,
captures a fresh checkpoint for the copy-on-write plan and journal, and persists
that derived state as a new immutable orchestration snapshot. The source
persisted state remains unchanged and cannot be reused as the destination.

The persisted directory uses the existing ELMAN-OS orchestration-state manifest
and file layout, so the standard read-only restoration and state-index
boundaries can consume it. No step or agent is executed and no network or AI
provider call occurs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_orchestration_selected_state_resume_application import (
    ArtifactOrchestrationSelectedStateResumeApplicationError,
    ArtifactOrchestrationSelectedStateResumeApplicationResult,
    ArtifactOrchestrationSelectedStateResumeApplicationStatus,
)
from .artifact_orchestration_state_persistence import (
    CHECKPOINT_FILE_NAME,
    JOURNAL_FILE_NAME,
    MANIFEST_FILE_NAME,
    PLAN_FILE_NAME,
    ArtifactOrchestrationPersistenceFile,
    ArtifactOrchestrationPersistencePolicy,
    ArtifactOrchestrationPersistenceResult,
    ArtifactOrchestrationPersistenceStatus,
    ArtifactOrchestrationStateManifest,
)
from .execution_checkpoint import (
    CheckpointIntegrityError,
    ExecutionCheckpoint,
    ExecutionCheckpointError,
)
from .execution_journal import ExecutionJournal, ExecutionJournalError
from .execution_plan import ExecutionPlan, ExecutionPlanError
from .path_security import is_trusted_macos_var_alias


ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION: Final[
    int
] = 1

_RESUME_STATE_STEP_ID: Final[str] = "resume-state"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_NAMES: Final[frozenset[str]] = frozenset(
    {
        PLAN_FILE_NAME,
        JOURNAL_FILE_NAME,
        CHECKPOINT_FILE_NAME,
        MANIFEST_FILE_NAME,
    }
)


class ArtifactOrchestrationSelectedStateResumePersistenceError(RuntimeError):
    """An applied resume state cannot be persisted safely."""


class ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError(
    ArtifactOrchestrationSelectedStateResumePersistenceError
):
    """The supplied application result does not authorize persistence."""


class ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
    ArtifactOrchestrationSelectedStateResumePersistenceError
):
    """A contract, source result, checkpoint, or persisted file is invalid."""


class ArtifactOrchestrationSelectedStateResumePersistenceLockError(
    ArtifactOrchestrationSelectedStateResumePersistenceError
):
    """The exclusive persistence lock cannot be acquired or released."""


class ArtifactOrchestrationSelectedStateResumePersistenceStatus(StrEnum):
    PERSISTED = "persisted"
    NOOP = "noop"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _payload_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be a non-empty payload string"
        )
    return value


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be a boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
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
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be a string or path-like value"
        ) from exc
    raw = _text(raw_value, name)
    path = Path(raw)
    if not path.is_absolute():
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"{name} must be absolute"
        )
    return path.as_posix()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_document(data: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json(data).encode("utf-8"))


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
        try:
            if component.exists() and component.is_symlink():
                if is_trusted_macos_var_alias(component):
                    continue
                raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                    f"symlink path component is forbidden: {component}"
                )
        except OSError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                f"cannot inspect path component: {component}"
            ) from exc


def _ensure_directory(path: Path, *, reject_symlinks: bool) -> None:
    if reject_symlinks:
        _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"persistence directory is invalid: {path}"
        )


def _fsync_directory(path: Path, *, required: bool) -> None:
    if not required:
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"cannot open directory for fsync: {path}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ArtifactOrchestrationSelectedStateResumePersistenceError(
            f"cannot fsync directory: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def _write_atomic(destination: Path, payload: bytes, *, fsync_file: bool) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
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
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"refusing to remove symlink staging path: {path}"
        )
    try:
        path.relative_to(expected_parent)
    except ValueError as exc:
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            "staging path escapes expected parent"
        ) from exc
    shutil.rmtree(path)


def _read_regular_file(path: Path, *, max_file_bytes: int) -> bytes:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"cannot inspect persisted file: {path.name}"
        ) from exc
    if stat.S_ISLNK(details.st_mode):
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"persisted symlink is forbidden: {path.name}"
        )
    if not stat.S_ISREG(details.st_mode):
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"persisted entry is not a regular file: {path.name}"
        )
    if details.st_size > max_file_bytes:
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"persisted file exceeds max_file_bytes: {path.name}"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"cannot read persisted file: {path.name}"
        ) from exc
    if len(payload) > max_file_bytes:
        raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
            f"persisted file exceeds max_file_bytes: {path.name}"
        )
    return payload


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumePersistencePolicy:
    policy_id: str
    persistence_policy_json: str
    persistence_policy_hash: str
    require_successful_application: bool = True
    require_new_persistence_id: bool = True
    require_source_immutability: bool = True
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        try:
            persistence_policy = ArtifactOrchestrationPersistencePolicy.from_json(
                _payload_text(self.persistence_policy_json, "persistence_policy_json")
            )
        except Exception as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "embedded persistence policy is invalid"
            ) from exc
        supplied_hash = _hash(
            self.persistence_policy_hash,
            "persistence_policy_hash",
        )
        if supplied_hash != persistence_policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persistence_policy_hash does not match embedded policy"
            )
        object.__setattr__(
            self,
            "persistence_policy_json",
            persistence_policy.to_json(),
        )
        object.__setattr__(self, "persistence_policy_hash", supplied_hash)
        for field_name in (
            "require_successful_application",
            "require_new_persistence_id",
            "require_source_immutability",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        if not self.require_successful_application:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "resume persistence must require a successful application"
            )
        if not self.require_new_persistence_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "resume persistence must require a new persistence identifier"
            )
        if not self.require_source_immutability:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "resume persistence must require source immutability"
            )
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "unsupported selected-state resume persistence format version"
            )

    @property
    def persistence_policy(self) -> ArtifactOrchestrationPersistencePolicy:
        return ArtifactOrchestrationPersistencePolicy.from_json(
            self.persistence_policy_json
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_persistence_policy"
            ),
            "version": self.version,
            "policy_id": self.policy_id,
            "persistence_policy_json": self.persistence_policy_json,
            "persistence_policy_hash": self.persistence_policy_hash,
            "require_successful_application": self.require_successful_application,
            "require_new_persistence_id": self.require_new_persistence_id,
            "require_source_immutability": self.require_source_immutability,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())

    @classmethod
    def from_persistence_policy(
        cls,
        *,
        policy_id: str,
        persistence_policy: ArtifactOrchestrationPersistencePolicy,
        require_successful_application: bool = True,
        require_new_persistence_id: bool = True,
        require_source_immutability: bool = True,
    ) -> "ArtifactOrchestrationSelectedStateResumePersistencePolicy":
        if not isinstance(
            persistence_policy,
            ArtifactOrchestrationPersistencePolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "persistence_policy must be an ArtifactOrchestrationPersistencePolicy"
            )
        return cls(
            policy_id=policy_id,
            persistence_policy_json=persistence_policy.to_json(),
            persistence_policy_hash=persistence_policy.policy_hash,
            require_successful_application=require_successful_application,
            require_new_persistence_id=require_new_persistence_id,
            require_source_immutability=require_source_immutability,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationSelectedStateResumePersistencePolicy":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_persistence_policy"
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_persistence_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            persistence_policy_json=data["persistence_policy_json"],
            persistence_policy_hash=data["persistence_policy_hash"],
            require_successful_application=data["require_successful_application"],
            require_new_persistence_id=data["require_new_persistence_id"],
            require_source_immutability=data["require_source_immutability"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumePersistencePolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumePersistenceRequest:
    persistence_request_id: str
    policy_json: str
    policy_hash: str
    application_result_json: str
    application_result_hash: str
    source_persistence_id: str
    persistence_id: str
    checkpoint_id: str
    state_root: str
    requested_by: str
    requested_at: str
    rationale: str
    request_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        for field_name in (
            "persistence_request_id",
            "source_persistence_id",
            "persistence_id",
            "checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        policy = ArtifactOrchestrationSelectedStateResumePersistencePolicy.from_json(
            _payload_text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        try:
            application = (
                ArtifactOrchestrationSelectedStateResumeApplicationResult.from_json(
                    _payload_text(
                        self.application_result_json,
                        "application_result_json",
                    )
                )
            )
            application.verify_hash()
        except ArtifactOrchestrationSelectedStateResumeApplicationError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "embedded selected-state resume application result is invalid"
            ) from exc
        supplied_application_hash = _hash(
            self.application_result_hash,
            "application_result_hash",
        )
        if supplied_application_hash != application.result_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "application_result_hash does not match embedded application"
            )
        object.__setattr__(self, "application_result_json", application.to_json())
        object.__setattr__(self, "application_result_hash", supplied_application_hash)

        embedded_source = _source_persistence_id(application)
        if self.source_persistence_id != embedded_source:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "source_persistence_id does not match embedded restored state"
            )
        if policy.require_new_persistence_id and self.persistence_id == embedded_source:
            raise ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError(
                "resume state must use a new persistence identifier"
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
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < application.completed_at:
            raise ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError(
                "resume persistence request cannot precede application"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "unsupported selected-state resume persistence format version"
            )

        expected_id = f"resume-persistence-request:{self.compute_identity_hash()}"
        if self.persistence_request_id != expected_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persistence_request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> ArtifactOrchestrationSelectedStateResumePersistencePolicy:
        return ArtifactOrchestrationSelectedStateResumePersistencePolicy.from_json(
            self.policy_json
        )

    @property
    def application_result(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeApplicationResult:
        return ArtifactOrchestrationSelectedStateResumeApplicationResult.from_json(
            self.application_result_json
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "application_result_hash": self.application_result_hash,
            "source_persistence_id": self.source_persistence_id,
            "persistence_id": self.persistence_id,
            "checkpoint_id": self.checkpoint_id,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_persistence_request"
            ),
            "version": self.version,
            "persistence_request_id": self.persistence_request_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "application_result_json": self.application_result_json,
            "application_result_hash": self.application_result_hash,
            "source_persistence_id": self.source_persistence_id,
            "persistence_id": self.persistence_id,
            "checkpoint_id": self.checkpoint_id,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "request hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_application_result(
        cls,
        *,
        application_result: ArtifactOrchestrationSelectedStateResumeApplicationResult,
        policy: ArtifactOrchestrationSelectedStateResumePersistencePolicy,
        state_root: str | Path,
        requested_by: str,
        requested_at: str | datetime,
        rationale: str,
        persistence_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> "ArtifactOrchestrationSelectedStateResumePersistenceRequest":
        if not isinstance(
            application_result,
            ArtifactOrchestrationSelectedStateResumeApplicationResult,
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "application_result must be an "
                "ArtifactOrchestrationSelectedStateResumeApplicationResult"
            )
        if not isinstance(
            policy,
            ArtifactOrchestrationSelectedStateResumePersistencePolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumePersistencePolicy"
            )
        application_result.verify_hash()
        normalized_root = _absolute_path(state_root, "state_root")
        normalized_requested_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_requested_at = _utc_timestamp(requested_at, "requested_at")
        normalized_rationale = _text(rationale, "rationale")
        source_id = _source_persistence_id(application_result)
        seed = _sha256_document(
            {
                "policy_hash": policy.policy_hash,
                "application_result_hash": application_result.result_hash,
                "source_persistence_id": source_id,
                "state_root": normalized_root,
                "requested_by": normalized_requested_by,
                "requested_at": normalized_requested_at,
                "rationale": normalized_rationale,
            }
        )
        destination_id = _identifier(
            persistence_id or f"resume-state:{seed}",
            "persistence_id",
        )
        destination_checkpoint_id = _identifier(
            checkpoint_id or f"resume-checkpoint:{seed}",
            "checkpoint_id",
        )
        identity_hash = _sha256_document(
            {
                "policy_hash": policy.policy_hash,
                "application_result_hash": application_result.result_hash,
                "source_persistence_id": source_id,
                "persistence_id": destination_id,
                "checkpoint_id": destination_checkpoint_id,
                "state_root": normalized_root,
                "requested_by": normalized_requested_by,
                "requested_at": normalized_requested_at,
                "rationale": normalized_rationale,
            }
        )
        return cls(
            persistence_request_id=f"resume-persistence-request:{identity_hash}",
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            application_result_json=application_result.to_json(),
            application_result_hash=application_result.result_hash or "",
            source_persistence_id=source_id,
            persistence_id=destination_id,
            checkpoint_id=destination_checkpoint_id,
            state_root=normalized_root,
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            rationale=normalized_rationale,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationSelectedStateResumePersistenceRequest":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_persistence_request"
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_persistence_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            persistence_request_id=data["persistence_request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            application_result_json=data["application_result_json"],
            application_result_hash=data["application_result_hash"],
            source_persistence_id=data["source_persistence_id"],
            persistence_id=data["persistence_id"],
            checkpoint_id=data["checkpoint_id"],
            state_root=data["state_root"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            rationale=data["rationale"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumePersistenceRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence request JSON must be an object"
            )
        return cls.from_dict(data)


def _source_persistence_id(
    application_result: ArtifactOrchestrationSelectedStateResumeApplicationResult,
) -> str:
    return (
        application_result.application_request.authorization_result
        .authorization_request.restoration_result.restored_state.persistence_id
    )


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumePersistenceResult:
    persistence_request_id: str
    status: ArtifactOrchestrationSelectedStateResumePersistenceStatus
    persistence_request_json: str
    checkpoint_json: str
    checkpoint_hash: str
    persistence_result_json: str
    persistence_result_hash: str
    source_persistence_id: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persistence_request_id",
            _identifier(self.persistence_request_id, "persistence_request_id"),
        )
        try:
            status = ArtifactOrchestrationSelectedStateResumePersistenceStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        request = ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_json(
            _payload_text(self.persistence_request_json, "persistence_request_json")
        )
        request.verify_hash()
        if request.persistence_request_id != self.persistence_request_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persistence_request_id does not match embedded request"
            )
        object.__setattr__(self, "persistence_request_json", request.to_json())

        try:
            checkpoint = ExecutionCheckpoint.from_json(
                _payload_text(self.checkpoint_json, "checkpoint_json")
            )
            checkpoint.verify_hash()
        except ExecutionCheckpointError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "embedded resume checkpoint is invalid"
            ) from exc
        supplied_checkpoint_hash = _hash(self.checkpoint_hash, "checkpoint_hash")
        if supplied_checkpoint_hash != checkpoint.checkpoint_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "checkpoint_hash does not match embedded checkpoint"
            )
        if checkpoint.checkpoint_id != request.checkpoint_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "checkpoint_id does not match persistence request"
            )
        object.__setattr__(self, "checkpoint_json", checkpoint.to_json())
        object.__setattr__(self, "checkpoint_hash", supplied_checkpoint_hash)

        try:
            persistence = ArtifactOrchestrationPersistenceResult.from_json(
                _payload_text(self.persistence_result_json, "persistence_result_json")
            )
            persistence.verify_hash()
        except Exception as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "embedded orchestration persistence result is invalid"
            ) from exc
        supplied_persistence_hash = _hash(
            self.persistence_result_hash,
            "persistence_result_hash",
        )
        if supplied_persistence_hash != persistence.result_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persistence_result_hash does not match embedded result"
            )
        if persistence.persistence_id != request.persistence_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted identifier does not match request"
            )
        if persistence.manifest.result_checkpoint_hash != supplied_checkpoint_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "manifest checkpoint hash does not match captured checkpoint"
            )
        object.__setattr__(self, "persistence_result_json", persistence.to_json())
        object.__setattr__(self, "persistence_result_hash", supplied_persistence_hash)

        expected_status = (
            ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED
            if persistence.status is ArtifactOrchestrationPersistenceStatus.PERSISTED
            else ArtifactOrchestrationSelectedStateResumePersistenceStatus.NOOP
        )
        if status is not expected_status:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "boundary status does not match persistence result"
            )
        source_id = _identifier(
            self.source_persistence_id,
            "source_persistence_id",
        )
        if source_id != request.source_persistence_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "source_persistence_id does not match request"
            )
        object.__setattr__(self, "source_persistence_id", source_id)
        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at != persistence.completed_at:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "completed_at does not match persistence result"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_PERSISTENCE_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "unsupported selected-state resume persistence format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def persistence_request(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumePersistenceRequest:
        return ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_json(
            self.persistence_request_json
        )

    @property
    def checkpoint(self) -> ExecutionCheckpoint:
        return ExecutionCheckpoint.from_json(self.checkpoint_json)

    @property
    def persistence_result(self) -> ArtifactOrchestrationPersistenceResult:
        return ArtifactOrchestrationPersistenceResult.from_json(
            self.persistence_result_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_persistence_result"
            ),
            "version": self.version,
            "persistence_request_id": self.persistence_request_id,
            "status": self.status.value,
            "persistence_request_json": self.persistence_request_json,
            "checkpoint_json": self.checkpoint_json,
            "checkpoint_hash": self.checkpoint_hash,
            "persistence_result_json": self.persistence_result_json,
            "persistence_result_hash": self.persistence_result_hash,
            "source_persistence_id": self.source_persistence_id,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateResumePersistenceResult":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_persistence_result"
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_persistence_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            persistence_request_id=data["persistence_request_id"],
            status=data["status"],
            persistence_request_json=data["persistence_request_json"],
            checkpoint_json=data["checkpoint_json"],
            checkpoint_hash=data["checkpoint_hash"],
            persistence_result_json=data["persistence_result_json"],
            persistence_result_hash=data["persistence_result_hash"],
            source_persistence_id=data["source_persistence_id"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumePersistenceResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "selected-state resume persistence result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumePersistence:
    request: ArtifactOrchestrationSelectedStateResumePersistenceRequest
    policy: ArtifactOrchestrationSelectedStateResumePersistencePolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationSelectedStateResumePersistenceRequest,
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "request must be an "
                "ArtifactOrchestrationSelectedStateResumePersistenceRequest"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationSelectedStateResumePersistencePolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumePersistencePolicy"
            )
        self.request.verify_hash()
        if self.request.policy_hash != self.policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                "request policy_hash does not match supplied policy"
            )
        if self.request.policy != self.policy:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "embedded request policy differs from supplied policy"
            )

    @property
    def state_root(self) -> Path:
        return Path(self.request.state_root)

    @property
    def state_key(self) -> str:
        return _sha256_bytes(self.request.persistence_id.encode("utf-8"))

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
        return self.state_root / ".locks" / f"{self.state_key}.lock"

    def persist(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumePersistenceResult:
        self.request.verify_hash()
        application = self.request.application_result
        application.verify_hash()
        if self.policy.require_successful_application and application.status not in {
            ArtifactOrchestrationSelectedStateResumeApplicationStatus.APPLIED,
            ArtifactOrchestrationSelectedStateResumeApplicationStatus.ALREADY_APPLIED,
        }:
            raise ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError(
                "selected-state resume application was not successful"
            )

        source = (
            application.application_request.authorization_result
            .authorization_request.restoration_result.restored_state
        )
        source.verify_hash()
        before = {
            "application": application.to_json(),
            "source": source.to_json(),
            "source_plan": source.plan.to_json(),
            "source_journal": source.journal.to_jsonl(),
            "source_checkpoint": source.checkpoint.to_json(),
        }

        plan = application.updated_plan
        journal = application.updated_journal
        try:
            journal.validate()
            checkpoint = ExecutionCheckpoint.capture(
                plan,
                journal,
                checkpoint_id=self.request.checkpoint_id,
                created_at=self.request.requested_at,
            )
            checkpoint.verify_hash()
        except (
            ExecutionPlanError,
            ExecutionJournalError,
            ExecutionCheckpointError,
            CheckpointIntegrityError,
        ) as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "updated resume state cannot produce a valid checkpoint"
            ) from exc

        prepared = {
            "application": application.to_json(),
            "source": source.to_json(),
            "source_plan": source.plan.to_json(),
            "source_journal": source.journal.to_jsonl(),
            "source_checkpoint": source.checkpoint.to_json(),
        }
        if self.policy.require_source_immutability and prepared != before:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "resume persistence preparation mutated the source restored state"
            )

        payloads = self._payloads(plan, journal, checkpoint)
        manifest = self._manifest(application, plan, journal, checkpoint, payloads)
        persistence = self._persist_payloads(payloads, manifest)

        after = {
            "application": application.to_json(),
            "source": source.to_json(),
            "source_plan": source.plan.to_json(),
            "source_journal": source.journal.to_jsonl(),
            "source_checkpoint": source.checkpoint.to_json(),
        }
        if self.policy.require_source_immutability and after != before:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "resume persistence mutated the source restored state"
            )

        status = (
            ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED
            if persistence.status is ArtifactOrchestrationPersistenceStatus.PERSISTED
            else ArtifactOrchestrationSelectedStateResumePersistenceStatus.NOOP
        )
        return ArtifactOrchestrationSelectedStateResumePersistenceResult(
            persistence_request_id=self.request.persistence_request_id,
            status=status,
            persistence_request_json=self.request.to_json(),
            checkpoint_json=checkpoint.to_json(),
            checkpoint_hash=checkpoint.checkpoint_hash or "",
            persistence_result_json=persistence.to_json(),
            persistence_result_hash=persistence.result_hash or "",
            source_persistence_id=self.request.source_persistence_id,
            completed_at=persistence.completed_at,
            reason=(
                "PERSISTED: applied resume state was committed as a new "
                "immutable orchestration snapshot"
                if status
                is ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED
                else "NOOP: the identical immutable resume snapshot already exists"
            ),
        )

    def _payloads(
        self,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        checkpoint: ExecutionCheckpoint,
    ) -> dict[str, bytes]:
        payloads = {
            PLAN_FILE_NAME: plan.to_json().encode("utf-8"),
            JOURNAL_FILE_NAME: journal.to_jsonl().encode("utf-8"),
            CHECKPOINT_FILE_NAME: checkpoint.to_json().encode("utf-8"),
        }
        limit = self.policy.persistence_policy.max_file_bytes
        for name, payload in payloads.items():
            if len(payload) > limit:
                raise ArtifactOrchestrationSelectedStateResumePersistenceError(
                    f"{name} exceeds persistence policy max_file_bytes"
                )
        return payloads

    def _manifest(
        self,
        application: ArtifactOrchestrationSelectedStateResumeApplicationResult,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        checkpoint: ExecutionCheckpoint,
        payloads: Mapping[str, bytes],
    ) -> ArtifactOrchestrationStateManifest:
        request_hash = self.request.request_hash
        application_hash = application.result_hash
        checkpoint_hash = checkpoint.checkpoint_hash
        assert request_hash is not None
        assert application_hash is not None
        assert checkpoint_hash is not None
        seal = journal.seal()
        command = (
            application.application_request.authorization_result.command
        )
        if command is None:
            raise ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError(
                "successful application is missing its authorized resume command"
            )
        command.verify_hash()
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
            orchestration_id=application.application_request_id,
            orchestration_result_hash=application_hash,
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            step_id=_RESUME_STATE_STEP_ID,
            agent_id=self.request.requested_by,
            transaction_id=command.command_id,
            result_plan_state_hash=_sha256_bytes(payloads[PLAN_FILE_NAME]),
            result_journal_hash=seal.journal_hash,
            result_checkpoint_hash=checkpoint_hash,
            files=entries,
            persisted_at=self.request.requested_at,
        )

    def _persist_payloads(
        self,
        payloads: Mapping[str, bytes],
        manifest: ArtifactOrchestrationStateManifest,
    ) -> ArtifactOrchestrationPersistenceResult:
        persistence_policy = self.policy.persistence_policy
        root = self.state_root
        if persistence_policy.reject_symlink_components:
            _reject_symlink_components(root)
        _ensure_directory(
            root,
            reject_symlinks=persistence_policy.reject_symlink_components,
        )
        _ensure_directory(
            self.lock_path.parent,
            reject_symlinks=persistence_policy.reject_symlink_components,
        )
        _ensure_directory(
            self.staging_directory.parent,
            reject_symlinks=persistence_policy.reject_symlink_components,
        )
        lock_payload = self._acquire_lock()
        created_staging = False
        committed = False
        try:
            if self.state_directory.exists():
                self._verify_state_directory(self.state_directory, payloads, manifest)
                return self._persistence_result(
                    ArtifactOrchestrationPersistenceStatus.NOOP,
                    manifest,
                    reason=(
                        "NOOP: identical applied resume state is already persisted"
                    ),
                )
            if self.staging_directory.exists():
                self._verify_state_directory(self.staging_directory, payloads, manifest)
            else:
                self.staging_directory.mkdir(parents=False, exist_ok=False)
                created_staging = True
                self._write_staging(payloads, manifest)
            if self.state_directory.exists():
                raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                    "final state directory appeared during persistence"
                )
            os.rename(self.staging_directory, self.state_directory)
            committed = True
            _fsync_directory(
                root,
                required=persistence_policy.require_directory_fsync,
            )
            self._verify_state_directory(self.state_directory, payloads, manifest)
            return self._persistence_result(
                ArtifactOrchestrationPersistenceStatus.PERSISTED,
                manifest,
                reason=(
                    "PERSISTED: resumed plan, journal, fresh checkpoint, and "
                    "manifest were committed atomically"
                ),
            )
        except Exception:
            if created_staging and not committed and self.staging_directory.exists():
                _safe_remove_tree(
                    self.staging_directory,
                    expected_parent=self.staging_directory.parent,
                )
            raise
        finally:
            self._release_lock(lock_payload)

    def _write_staging(
        self,
        payloads: Mapping[str, bytes],
        manifest: ArtifactOrchestrationStateManifest,
    ) -> None:
        persistence_policy = self.policy.persistence_policy
        for name in (PLAN_FILE_NAME, JOURNAL_FILE_NAME, CHECKPOINT_FILE_NAME):
            _write_atomic(
                self.staging_directory / name,
                payloads[name],
                fsync_file=persistence_policy.fsync_files,
            )
        _write_atomic(
            self.staging_directory / MANIFEST_FILE_NAME,
            manifest.to_json().encode("utf-8"),
            fsync_file=persistence_policy.fsync_files,
        )
        _fsync_directory(
            self.staging_directory,
            required=persistence_policy.require_directory_fsync,
        )
        self._verify_state_directory(self.staging_directory, payloads, manifest)

    def _verify_state_directory(
        self,
        directory: Path,
        payloads: Mapping[str, bytes],
        manifest: ArtifactOrchestrationStateManifest,
    ) -> None:
        if not directory.is_dir() or directory.is_symlink():
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted state directory is invalid"
            )
        try:
            names = {item.name for item in directory.iterdir()}
        except OSError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "cannot enumerate persisted state directory"
            ) from exc
        if names != _EXPECTED_NAMES:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted state directory contains unexpected entries"
            )
        limit = self.policy.persistence_policy.max_file_bytes
        for name, expected in payloads.items():
            actual = _read_regular_file(directory / name, max_file_bytes=limit)
            if actual != expected:
                raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                    f"persisted payload differs from expected content: {name}"
                )
        manifest_payload = _read_regular_file(
            directory / MANIFEST_FILE_NAME,
            max_file_bytes=limit,
        )
        expected_manifest = manifest.to_json().encode("utf-8")
        if manifest_payload != expected_manifest:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted manifest differs from expected content"
            )
        try:
            plan = ExecutionPlan.from_json(payloads[PLAN_FILE_NAME].decode("utf-8"))
            journal = ExecutionJournal.from_jsonl(
                payloads[JOURNAL_FILE_NAME].decode("utf-8")
            )
            checkpoint = ExecutionCheckpoint.from_json(
                payloads[CHECKPOINT_FILE_NAME].decode("utf-8")
            )
            journal.validate()
            checkpoint.verify_hash()
        except (
            UnicodeDecodeError,
            ExecutionPlanError,
            ExecutionJournalError,
            ExecutionCheckpointError,
        ) as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted resume state cannot be reconstructed"
            ) from exc
        if plan.to_json().encode("utf-8") != payloads[PLAN_FILE_NAME]:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted plan is not canonical"
            )
        if journal.to_jsonl().encode("utf-8") != payloads[JOURNAL_FILE_NAME]:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted journal is not canonical"
            )
        if checkpoint.to_json().encode("utf-8") != payloads[CHECKPOINT_FILE_NAME]:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted checkpoint is not canonical"
            )
        if checkpoint.plan_id != plan.plan_id or journal.plan_id != plan.plan_id:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted plan, journal, and checkpoint identifiers diverge"
            )
        if checkpoint.checkpoint_hash != manifest.result_checkpoint_hash:
            raise ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError(
                "persisted checkpoint hash differs from manifest"
            )
        manifest.verify_hash()

    def _lock_document(self) -> dict[str, Any]:
        return {
            "record_type": "selected_state_resume_persistence_lock",
            "persistence_request_id": self.request.persistence_request_id,
            "persistence_id": self.request.persistence_id,
            "request_hash": self.request.request_hash,
        }

    def _acquire_lock(self) -> bytes:
        payload = canonical_json(self._lock_document()).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor: int | None = None
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                if self.policy.persistence_policy.fsync_files:
                    os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                "resume persistence lock already exists"
            ) from exc
        except OSError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                "cannot acquire resume persistence lock"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return payload

    def _release_lock(self, expected_payload: bytes) -> None:
        try:
            if not self.lock_path.exists():
                raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                    "resume persistence lock disappeared before release"
                )
            if self.lock_path.is_symlink():
                raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                    "resume persistence lock became a symlink"
                )
            actual = self.lock_path.read_bytes()
            if actual != expected_payload:
                raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                    "resume persistence lock content changed"
                )
            self.lock_path.unlink()
        except ArtifactOrchestrationSelectedStateResumePersistenceLockError:
            raise
        except OSError as exc:
            raise ArtifactOrchestrationSelectedStateResumePersistenceLockError(
                "cannot release resume persistence lock"
            ) from exc

    def _persistence_result(
        self,
        status: ArtifactOrchestrationPersistenceStatus,
        manifest: ArtifactOrchestrationStateManifest,
        *,
        reason: str,
    ) -> ArtifactOrchestrationPersistenceResult:
        manifest_hash = manifest.manifest_hash
        application_hash = self.request.application_result_hash
        assert manifest_hash is not None
        return ArtifactOrchestrationPersistenceResult(
            persistence_id=self.request.persistence_id,
            status=status,
            request_hash=self.request.request_hash or "",
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            orchestration_id=self.request.application_result.application_request_id,
            orchestration_result_hash=application_hash,
            state_root=self.request.state_root,
            state_directory=self.state_directory.as_posix(),
            manifest_hash=manifest_hash,
            manifest_json=manifest.to_json(),
            completed_at=self.request.requested_at,
            reason=reason,
        )
