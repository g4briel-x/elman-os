"""Verified, read-only restoration of persisted ELMAN-OS orchestration state.

The restoration boundary locates a state directory by ``persistence_id``,
validates its manifest and all persisted payloads, reconstructs the execution
plan, append-only journal, and checkpoint, and evaluates whether the restored
checkpoint is terminal, blocked, or ready to resume.

The component is deliberately read-only. It never rewrites persisted state,
executes persisted content, imports persisted code, performs network access,
or invokes an AI provider.
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
    ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION,
    CHECKPOINT_FILE_NAME,
    JOURNAL_FILE_NAME,
    MANIFEST_FILE_NAME,
    PLAN_FILE_NAME,
    ArtifactOrchestrationPersistenceError,
    ArtifactOrchestrationPersistenceResult,
    ArtifactOrchestrationStateManifest,
)
from .execution_checkpoint import (
    ExecutionCheckpoint,
    ExecutionCheckpointError,
    ResumeAssessmentStatus,
)
from .execution_journal import (
    ExecutionJournal,
    ExecutionJournalError,
)
from .execution_plan import (
    ExecutionPlan,
    ExecutionPlanError,
)
from .path_security import is_trusted_macos_var_alias


ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION: Final[int] = 1

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


class ArtifactOrchestrationRestorationError(RuntimeError):
    """A restoration contract or read operation is invalid."""


class ArtifactOrchestrationRestorationNotFoundError(
    ArtifactOrchestrationRestorationError
):
    """The requested persisted orchestration state does not exist."""


class ArtifactOrchestrationRestorationIntegrityError(
    ArtifactOrchestrationRestorationError
):
    """Persisted orchestration state fails integrity verification."""


class ArtifactOrchestrationRestorationReadError(
    ArtifactOrchestrationRestorationError
):
    """A persisted file cannot be read within the configured boundary."""


class ArtifactOrchestrationRestorationStatus(StrEnum):
    RESTORED = "restored"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _payload_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be a non-empty string"
        )
    return value


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ArtifactOrchestrationRestorationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be a positive integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationRestorationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationRestorationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationRestorationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationRestorationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationRestorationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationRestorationError(
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
        raise ArtifactOrchestrationRestorationError(
            f"{name} must be a string or path-like value"
        ) from exc
    raw = _text(raw_value, name)
    path = Path(raw)
    if not path.is_absolute():
        raise ArtifactOrchestrationRestorationError(
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
                if is_trusted_macos_var_alias(component):
                    continue
                raise ArtifactOrchestrationRestorationIntegrityError(
                    f"symlink path component is forbidden: {component}"
                )
        except OSError as exc:
            raise ArtifactOrchestrationRestorationReadError(
                f"cannot inspect path component: {component}"
            ) from exc


def _read_regular_file(
    path: Path,
    *,
    max_file_bytes: int,
) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactOrchestrationRestorationNotFoundError(
            f"persisted file is missing: {path.name}"
        ) from exc
    except OSError as exc:
        raise ArtifactOrchestrationRestorationReadError(
            f"cannot inspect persisted file: {path.name}"
        ) from exc

    if stat.S_ISLNK(before.st_mode):
        raise ArtifactOrchestrationRestorationIntegrityError(
            f"persisted symlink is forbidden: {path.name}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactOrchestrationRestorationIntegrityError(
            f"persisted entry is not a regular file: {path.name}"
        )
    if before.st_size > max_file_bytes:
        raise ArtifactOrchestrationRestorationReadError(
            f"persisted file exceeds max_file_bytes: {path.name}"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ArtifactOrchestrationRestorationIntegrityError(
                f"opened entry is not a regular file: {path.name}"
            )
        if observed.st_size > max_file_bytes:
            raise ArtifactOrchestrationRestorationReadError(
                f"persisted file exceeds max_file_bytes: {path.name}"
            )
        if (
            before.st_dev != observed.st_dev
            or before.st_ino != observed.st_ino
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                f"persisted file changed during open: {path.name}"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_file_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_file_bytes:
                raise ArtifactOrchestrationRestorationReadError(
                    f"persisted file exceeds max_file_bytes: {path.name}"
                )
            chunks.append(chunk)
        payload = b"".join(chunks)
    except ArtifactOrchestrationRestorationError:
        raise
    except OSError as exc:
        raise ArtifactOrchestrationRestorationReadError(
            f"cannot read persisted file: {path.name}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        after = path.lstat()
    except OSError as exc:
        raise ArtifactOrchestrationRestorationReadError(
            f"cannot re-inspect persisted file: {path.name}"
        ) from exc

    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ArtifactOrchestrationRestorationIntegrityError(
            f"persisted file changed during read: {path.name}"
        )

    return payload


def _decode_utf8(payload: bytes, name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactOrchestrationRestorationIntegrityError(
            f"{name} is not valid UTF-8"
        ) from exc


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationRestorationPolicy:
    policy_id: str
    reject_symlink_components: bool = True
    require_exact_entry_set: bool = True
    require_canonical_payloads: bool = True
    require_compatible_checkpoint: bool = True
    max_file_bytes: int = 64 * 1024 * 1024
    version: int = ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION

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
            "max_file_bytes",
            _positive_int(self.max_file_bytes, "max_file_bytes"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION:
            raise ArtifactOrchestrationRestorationError(
                "unsupported restoration format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_restoration_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "reject_symlink_components": self.reject_symlink_components,
            "require_exact_entry_set": self.require_exact_entry_set,
            "require_canonical_payloads": self.require_canonical_payloads,
            "require_compatible_checkpoint": (
                self.require_compatible_checkpoint
            ),
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
    ) -> "ArtifactOrchestrationRestorationPolicy":
        if (
            data.get("record_type")
            != "artifact_orchestration_restoration_policy"
        ):
            raise ArtifactOrchestrationRestorationError(
                "record_type must be artifact_orchestration_restoration_policy"
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
            max_file_bytes=data["max_file_bytes"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationRestorationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationRestorationError(
                "restoration policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationRestorationError(
                "restoration policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationRestorationRequest:
    restoration_id: str
    policy_id: str
    policy_hash: str
    persistence_id: str
    state_root: str
    requested_by: str
    requested_at: str
    expected_manifest_hash: str | None = None
    expected_orchestration_result_hash: str | None = None
    request_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "restoration_id",
            "policy_id",
            "persistence_id",
        ):
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
            "requested_at",
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "expected_manifest_hash",
            _optional_hash(
                self.expected_manifest_hash,
                "expected_manifest_hash",
            ),
        )
        object.__setattr__(
            self,
            "expected_orchestration_result_hash",
            _optional_hash(
                self.expected_orchestration_result_hash,
                "expected_orchestration_result_hash",
            ),
        )
        if self.version != ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION:
            raise ArtifactOrchestrationRestorationError(
                "unsupported restoration format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_identifiers(
        cls,
        *,
        persistence_id: str,
        state_root: str | Path,
        policy: ArtifactOrchestrationRestorationPolicy,
        requested_by: str,
        requested_at: str | datetime,
        expected_manifest_hash: str | None = None,
        expected_orchestration_result_hash: str | None = None,
        restoration_id: str | None = None,
    ) -> "ArtifactOrchestrationRestorationRequest":
        if not isinstance(
            policy,
            ArtifactOrchestrationRestorationPolicy,
        ):
            raise ArtifactOrchestrationRestorationError(
                "policy must be an ArtifactOrchestrationRestorationPolicy"
            )
        normalized_persistence_id = _identifier(
            persistence_id,
            "persistence_id",
        )
        normalized_root = _absolute_path(state_root, "state_root")
        manifest_hash = _optional_hash(
            expected_manifest_hash,
            "expected_manifest_hash",
        )
        result_hash = _optional_hash(
            expected_orchestration_result_hash,
            "expected_orchestration_result_hash",
        )
        identity_hash = _sha256_document(
            {
                "record_type": "artifact_orchestration_restoration_identity",
                "policy_hash": policy.policy_hash,
                "persistence_id": normalized_persistence_id,
                "state_root": normalized_root,
                "expected_manifest_hash": manifest_hash,
                "expected_orchestration_result_hash": result_hash,
            }
        )
        effective_id = (
            restoration_id
            if restoration_id is not None
            else f"orchestration-restoration:{identity_hash}"
        )
        return cls(
            restoration_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            persistence_id=normalized_persistence_id,
            state_root=normalized_root,
            requested_by=requested_by,
            requested_at=requested_at,
            expected_manifest_hash=manifest_hash,
            expected_orchestration_result_hash=result_hash,
        )

    @classmethod
    def from_persistence_result(
        cls,
        persistence_result: ArtifactOrchestrationPersistenceResult,
        policy: ArtifactOrchestrationRestorationPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        restoration_id: str | None = None,
    ) -> "ArtifactOrchestrationRestorationRequest":
        if not isinstance(
            persistence_result,
            ArtifactOrchestrationPersistenceResult,
        ):
            raise ArtifactOrchestrationRestorationError(
                "persistence_result must be an "
                "ArtifactOrchestrationPersistenceResult"
            )
        persistence_result.verify_hash()
        return cls.from_identifiers(
            persistence_id=persistence_result.persistence_id,
            state_root=persistence_result.state_root,
            policy=policy,
            requested_by=requested_by,
            requested_at=requested_at,
            expected_manifest_hash=persistence_result.manifest_hash,
            expected_orchestration_result_hash=(
                persistence_result.orchestration_result_hash
            ),
            restoration_id=restoration_id,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_restoration_request",
            "version": self.version,
            "restoration_id": self.restoration_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "persistence_id": self.persistence_id,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "expected_manifest_hash": self.expected_manifest_hash,
            "expected_orchestration_result_hash": (
                self.expected_orchestration_result_hash
            ),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationRestorationIntegrityError(
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
    ) -> "ArtifactOrchestrationRestorationRequest":
        if (
            data.get("record_type")
            != "artifact_orchestration_restoration_request"
        ):
            raise ArtifactOrchestrationRestorationError(
                "record_type must be artifact_orchestration_restoration_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            restoration_id=data["restoration_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            persistence_id=data["persistence_id"],
            state_root=data["state_root"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            expected_manifest_hash=data.get("expected_manifest_hash"),
            expected_orchestration_result_hash=data.get(
                "expected_orchestration_result_hash"
            ),
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationRestorationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationRestorationError(
                "restoration request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationRestorationError(
                "restoration request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationRestoredState:
    persistence_id: str
    manifest_hash: str
    orchestration_result_hash: str
    plan_id: str
    project_id: str
    checkpoint_id: str
    plan_state_hash: str
    journal_hash: str
    checkpoint_hash: str
    plan_json: str
    journal_jsonl: str
    checkpoint_json: str
    assessment_status: ResumeAssessmentStatus
    can_resume: bool
    assessment_json: str
    restored_at: str
    state_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persistence_id",
            _identifier(self.persistence_id, "persistence_id"),
        )
        for field_name in (
            "manifest_hash",
            "orchestration_result_hash",
            "plan_state_hash",
            "journal_hash",
            "checkpoint_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        for field_name in (
            "plan_id",
            "project_id",
            "checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )

        plan_json = _payload_text(self.plan_json, "plan_json")
        journal_jsonl = _payload_text(
            self.journal_jsonl,
            "journal_jsonl",
        )
        checkpoint_json = _payload_text(
            self.checkpoint_json,
            "checkpoint_json",
        )
        assessment_json = _text(
            self.assessment_json,
            "assessment_json",
        )

        try:
            plan = ExecutionPlan.from_json(plan_json)
            journal = ExecutionJournal.from_jsonl(journal_jsonl)
            checkpoint = ExecutionCheckpoint.from_json(checkpoint_json)
        except (
            ExecutionPlanError,
            ExecutionJournalError,
            ExecutionCheckpointError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored state embeds invalid execution objects"
            ) from exc

        if plan.plan_id != self.plan_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded plan_id does not match restored state"
            )
        if plan.project_id != self.project_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded project_id does not match restored state"
            )
        if journal.plan_id != self.plan_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded journal plan_id does not match restored state"
            )
        if checkpoint.checkpoint_id != self.checkpoint_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded checkpoint_id does not match restored state"
            )

        computed_plan_hash = _sha256_bytes(plan_json.encode("utf-8"))
        seal = journal.seal()
        checkpoint.verify_hash()
        if computed_plan_hash != self.plan_state_hash:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded plan hash does not match restored state"
            )
        if seal.journal_hash != self.journal_hash:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded journal hash does not match restored state"
            )
        if checkpoint.checkpoint_hash != self.checkpoint_hash:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "embedded checkpoint hash does not match restored state"
            )

        try:
            assessment_data = json.loads(assessment_json)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "assessment_json is invalid"
            ) from exc
        if not isinstance(assessment_data, dict):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "assessment_json must be an object"
            )
        try:
            status = ResumeAssessmentStatus(self.assessment_status)
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationRestorationError(
                "assessment_status is invalid"
            ) from exc
        object.__setattr__(self, "assessment_status", status)

        if not isinstance(self.can_resume, bool):
            raise ArtifactOrchestrationRestorationError(
                "can_resume must be boolean"
            )
        if assessment_data.get("status") != status.value:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "assessment status does not match assessment_json"
            )
        if assessment_data.get("can_resume") is not self.can_resume:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "assessment can_resume does not match assessment_json"
            )
        if assessment_data.get("checkpoint_id") != self.checkpoint_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "assessment checkpoint_id does not match restored state"
            )

        object.__setattr__(self, "plan_json", plan_json)
        object.__setattr__(self, "journal_jsonl", journal_jsonl)
        object.__setattr__(self, "checkpoint_json", checkpoint_json)
        object.__setattr__(
            self,
            "assessment_json",
            canonical_json(assessment_data),
        )
        object.__setattr__(
            self,
            "restored_at",
            _utc_timestamp(self.restored_at, "restored_at"),
        )
        if self.version != ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION:
            raise ArtifactOrchestrationRestorationError(
                "unsupported restoration format version"
            )

        computed = self.compute_hash()
        if self.state_hash is None:
            object.__setattr__(self, "state_hash", computed)
        else:
            supplied = _hash(self.state_hash, "state_hash")
            if supplied != computed:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "state hash does not match restored state content"
                )
            object.__setattr__(self, "state_hash", supplied)

    @property
    def plan(self) -> ExecutionPlan:
        return ExecutionPlan.from_json(self.plan_json)

    @property
    def journal(self) -> ExecutionJournal:
        return ExecutionJournal.from_jsonl(self.journal_jsonl)

    @property
    def checkpoint(self) -> ExecutionCheckpoint:
        return ExecutionCheckpoint.from_json(self.checkpoint_json)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_restored_state",
            "version": self.version,
            "persistence_id": self.persistence_id,
            "manifest_hash": self.manifest_hash,
            "orchestration_result_hash": self.orchestration_result_hash,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "checkpoint_id": self.checkpoint_id,
            "plan_state_hash": self.plan_state_hash,
            "journal_hash": self.journal_hash,
            "checkpoint_hash": self.checkpoint_hash,
            "plan_json": self.plan_json,
            "journal_jsonl": self.journal_jsonl,
            "checkpoint_json": self.checkpoint_json,
            "assessment_status": self.assessment_status.value,
            "can_resume": self.can_resume,
            "assessment_json": self.assessment_json,
            "restored_at": self.restored_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.state_hash != self.compute_hash():
            raise ArtifactOrchestrationRestorationIntegrityError(
                "state hash does not match restored state content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["state_hash"] = self.state_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationRestoredState":
        if (
            data.get("record_type")
            != "artifact_orchestration_restored_state"
        ):
            raise ArtifactOrchestrationRestorationError(
                "record_type must be artifact_orchestration_restored_state"
            )
        if "state_hash" not in data:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "serialized restored state is missing state_hash"
            )
        return cls(
            persistence_id=data["persistence_id"],
            manifest_hash=data["manifest_hash"],
            orchestration_result_hash=data[
                "orchestration_result_hash"
            ],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            checkpoint_id=data["checkpoint_id"],
            plan_state_hash=data["plan_state_hash"],
            journal_hash=data["journal_hash"],
            checkpoint_hash=data["checkpoint_hash"],
            plan_json=data["plan_json"],
            journal_jsonl=data["journal_jsonl"],
            checkpoint_json=data["checkpoint_json"],
            assessment_status=ResumeAssessmentStatus(
                data["assessment_status"]
            ),
            can_resume=data["can_resume"],
            assessment_json=data["assessment_json"],
            restored_at=data["restored_at"],
            state_hash=data["state_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationRestoredState":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationRestorationError(
                "restored state JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationRestorationError(
                "restored state JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationRestorationResult:
    restoration_id: str
    status: ArtifactOrchestrationRestorationStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    persistence_id: str
    state_root: str
    state_directory: str
    manifest_hash: str
    orchestration_result_hash: str
    restored_state_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "restoration_id",
            "policy_id",
            "persistence_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        try:
            status = ArtifactOrchestrationRestorationStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationRestorationError(
                "restoration status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        for field_name in (
            "request_hash",
            "policy_hash",
            "manifest_hash",
            "orchestration_result_hash",
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
            raise ArtifactOrchestrationRestorationIntegrityError(
                "state_directory escapes state_root"
            ) from exc
        object.__setattr__(self, "state_root", root)
        object.__setattr__(self, "state_directory", directory)

        restored_json = _text(
            self.restored_state_json,
            "restored_state_json",
        )
        restored = ArtifactOrchestrationRestoredState.from_json(
            restored_json
        )
        restored.verify_hash()
        if restored.persistence_id != self.persistence_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored state persistence_id does not match result"
            )
        if restored.manifest_hash != self.manifest_hash:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored state manifest_hash does not match result"
            )
        if (
            restored.orchestration_result_hash
            != self.orchestration_result_hash
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored orchestration result hash does not match result"
            )
        object.__setattr__(
            self,
            "restored_state_json",
            restored.to_json(),
        )
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
        if self.version != ARTIFACT_ORCHESTRATION_RESTORATION_FORMAT_VERSION:
            raise ArtifactOrchestrationRestorationError(
                "unsupported restoration format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "result hash does not match restoration result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def restored_state(self) -> ArtifactOrchestrationRestoredState:
        return ArtifactOrchestrationRestoredState.from_json(
            self.restored_state_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_restoration_result",
            "version": self.version,
            "restoration_id": self.restoration_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "persistence_id": self.persistence_id,
            "state_root": self.state_root,
            "state_directory": self.state_directory,
            "manifest_hash": self.manifest_hash,
            "orchestration_result_hash": (
                self.orchestration_result_hash
            ),
            "restored_state_json": self.restored_state_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationRestorationIntegrityError(
                "result hash does not match restoration result content"
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
    ) -> "ArtifactOrchestrationRestorationResult":
        if (
            data.get("record_type")
            != "artifact_orchestration_restoration_result"
        ):
            raise ArtifactOrchestrationRestorationError(
                "record_type must be artifact_orchestration_restoration_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            restoration_id=data["restoration_id"],
            status=ArtifactOrchestrationRestorationStatus(
                data["status"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            persistence_id=data["persistence_id"],
            state_root=data["state_root"],
            state_directory=data["state_directory"],
            manifest_hash=data["manifest_hash"],
            orchestration_result_hash=data[
                "orchestration_result_hash"
            ],
            restored_state_json=data["restored_state_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationRestorationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationRestorationError(
                "restoration result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationRestorationError(
                "restoration result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateRestoration:
    request: ArtifactOrchestrationRestorationRequest
    policy: ArtifactOrchestrationRestorationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationRestorationRequest,
        ):
            raise ArtifactOrchestrationRestorationError(
                "request must be an ArtifactOrchestrationRestorationRequest"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationRestorationPolicy,
        ):
            raise ArtifactOrchestrationRestorationError(
                "policy must be an ArtifactOrchestrationRestorationPolicy"
            )
        self.request.verify_hash()
        if self.request.policy_id != self.policy.policy_id:
            raise ArtifactOrchestrationRestorationError(
                "request policy_id does not match policy"
            )
        if self.request.policy_hash != self.policy.policy_hash:
            raise ArtifactOrchestrationRestorationError(
                "request policy_hash does not match policy"
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

    def restore(self) -> ArtifactOrchestrationRestorationResult:
        root = self.state_root
        directory = self.state_directory

        if self.policy.reject_symlink_components:
            _reject_symlink_components(root)
            _reject_symlink_components(directory)

        if not root.exists():
            raise ArtifactOrchestrationRestorationNotFoundError(
                "state_root does not exist"
            )
        if not root.is_dir() or root.is_symlink():
            raise ArtifactOrchestrationRestorationIntegrityError(
                "state_root is not a regular directory"
            )
        if not directory.exists():
            raise ArtifactOrchestrationRestorationNotFoundError(
                "persisted orchestration state does not exist"
            )
        if not directory.is_dir() or directory.is_symlink():
            raise ArtifactOrchestrationRestorationIntegrityError(
                "state directory is not a regular directory"
            )

        names = self._entry_names(directory)
        if self.policy.require_exact_entry_set:
            if names != _EXPECTED_NAMES:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "state directory contains missing or unexpected entries"
                )
        elif not _EXPECTED_NAMES.issubset(names):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "state directory is missing required entries"
            )

        manifest_payload = _read_regular_file(
            directory / MANIFEST_FILE_NAME,
            max_file_bytes=self.policy.max_file_bytes,
        )
        manifest_text = _decode_utf8(
            manifest_payload,
            MANIFEST_FILE_NAME,
        )
        try:
            manifest = ArtifactOrchestrationStateManifest.from_json(
                manifest_text
            )
            manifest.verify_hash()
        except ArtifactOrchestrationPersistenceError as exc:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "persisted manifest is invalid"
            ) from exc

        if self.policy.require_canonical_payloads:
            if manifest.to_json() != manifest_text:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "persisted manifest is not canonical"
                )

        self._verify_manifest_binding(manifest)

        payloads: dict[str, bytes] = {}
        entries = {entry.path: entry for entry in manifest.files}
        for name in (
            PLAN_FILE_NAME,
            JOURNAL_FILE_NAME,
            CHECKPOINT_FILE_NAME,
        ):
            entry = entries[name]
            if entry.size_bytes > self.policy.max_file_bytes:
                raise ArtifactOrchestrationRestorationReadError(
                    f"manifest file exceeds max_file_bytes: {name}"
                )
            payload = _read_regular_file(
                directory / name,
                max_file_bytes=self.policy.max_file_bytes,
            )
            if len(payload) != entry.size_bytes:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    f"persisted file size differs from manifest: {name}"
                )
            if _sha256_bytes(payload) != entry.sha256:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    f"persisted file hash differs from manifest: {name}"
                )
            payloads[name] = payload

        plan_text = _decode_utf8(
            payloads[PLAN_FILE_NAME],
            PLAN_FILE_NAME,
        )
        journal_text = _decode_utf8(
            payloads[JOURNAL_FILE_NAME],
            JOURNAL_FILE_NAME,
        )
        checkpoint_text = _decode_utf8(
            payloads[CHECKPOINT_FILE_NAME],
            CHECKPOINT_FILE_NAME,
        )

        try:
            plan = ExecutionPlan.from_json(plan_text)
            journal = ExecutionJournal.from_jsonl(journal_text)
            checkpoint = ExecutionCheckpoint.from_json(checkpoint_text)
            checkpoint.verify_hash()
            seal = journal.seal()
        except (
            ExecutionPlanError,
            ExecutionJournalError,
            ExecutionCheckpointError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "persisted execution state cannot be reconstructed"
            ) from exc

        if self.policy.require_canonical_payloads:
            if plan.to_json() != plan_text:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "persisted execution plan is not canonical"
                )
            if journal.to_jsonl() != journal_text:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "persisted execution journal is not canonical"
                )
            if checkpoint.to_json() != checkpoint_text:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "persisted execution checkpoint is not canonical"
                )

        if plan.plan_id != manifest.plan_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest plan_id differs from restored plan"
            )
        if plan.project_id != manifest.project_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest project_id differs from restored plan"
            )
        if journal.plan_id != manifest.plan_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest plan_id differs from restored journal"
            )
        if checkpoint.plan_id != manifest.plan_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest plan_id differs from restored checkpoint"
            )
        if checkpoint.project_id != manifest.project_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest project_id differs from restored checkpoint"
            )

        if _sha256_bytes(plan_text.encode("utf-8")) != (
            manifest.result_plan_state_hash
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored plan hash differs from manifest"
            )
        if seal.journal_hash != manifest.result_journal_hash:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored journal hash differs from manifest"
            )
        if checkpoint.checkpoint_hash != (
            manifest.result_checkpoint_hash
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "restored checkpoint hash differs from manifest"
            )

        assessment = checkpoint.assess_resume(plan, journal)
        if self.policy.require_compatible_checkpoint:
            if assessment.status in {
                ResumeAssessmentStatus.STALE,
                ResumeAssessmentStatus.INCOMPATIBLE,
            }:
                raise ArtifactOrchestrationRestorationIntegrityError(
                    "restored checkpoint is not compatible with plan and journal"
                )

        manifest_hash = manifest.manifest_hash
        checkpoint_hash = checkpoint.checkpoint_hash
        assert manifest_hash is not None
        assert checkpoint_hash is not None

        restored_state = ArtifactOrchestrationRestoredState(
            persistence_id=self.request.persistence_id,
            manifest_hash=manifest_hash,
            orchestration_result_hash=(
                manifest.orchestration_result_hash
            ),
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            checkpoint_id=checkpoint.checkpoint_id,
            plan_state_hash=manifest.result_plan_state_hash,
            journal_hash=seal.journal_hash,
            checkpoint_hash=checkpoint_hash,
            plan_json=plan_text,
            journal_jsonl=journal_text,
            checkpoint_json=checkpoint_text,
            assessment_status=assessment.status,
            can_resume=assessment.can_resume,
            assessment_json=assessment.to_json(),
            restored_at=self.request.requested_at,
        )

        request_hash = self.request.request_hash
        assert request_hash is not None
        return ArtifactOrchestrationRestorationResult(
            restoration_id=self.request.restoration_id,
            status=ArtifactOrchestrationRestorationStatus.RESTORED,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            persistence_id=self.request.persistence_id,
            state_root=root.as_posix(),
            state_directory=directory.as_posix(),
            manifest_hash=manifest_hash,
            orchestration_result_hash=(
                manifest.orchestration_result_hash
            ),
            restored_state_json=restored_state.to_json(),
            completed_at=self.request.requested_at,
            reason=(
                "RESTORED: manifest, plan, journal, checkpoint, and "
                "resume assessment were verified without modifying state"
            ),
        )

    def _entry_names(self, directory: Path) -> frozenset[str]:
        names: set[str] = set()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise ArtifactOrchestrationRestorationIntegrityError(
                            f"state directory contains symlink: {entry.name}"
                        )
                    names.add(entry.name)
        except ArtifactOrchestrationRestorationError:
            raise
        except OSError as exc:
            raise ArtifactOrchestrationRestorationReadError(
                "cannot enumerate state directory"
            ) from exc
        return frozenset(names)

    def _verify_manifest_binding(
        self,
        manifest: ArtifactOrchestrationStateManifest,
    ) -> None:
        if manifest.version != (
            ARTIFACT_ORCHESTRATION_PERSISTENCE_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest persistence format version is unsupported"
            )
        if manifest.persistence_id != self.request.persistence_id:
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest persistence_id differs from request"
            )
        if (
            self.request.expected_manifest_hash is not None
            and manifest.manifest_hash
            != self.request.expected_manifest_hash
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest hash differs from expected_manifest_hash"
            )
        if (
            self.request.expected_orchestration_result_hash is not None
            and manifest.orchestration_result_hash
            != self.request.expected_orchestration_result_hash
        ):
            raise ArtifactOrchestrationRestorationIntegrityError(
                "manifest orchestration result hash differs from expectation"
            )
