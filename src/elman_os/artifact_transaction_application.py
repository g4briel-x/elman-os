"""Transactional artifact application for ELMAN-OS v0.7.

This module performs the first controlled write stage of the v0.7
orchestration pipeline. It accepts only a ready application plan, verified
payload bytes, and a ready read-only workspace preflight result.

The implementation is deliberately fail-closed:

* an exclusive transaction lock is acquired inside the workspace;
* the preflight snapshot is revalidated immediately before writes;
* update targets are backed up before replacement;
* create targets use an atomic no-overwrite hard-link commit;
* update targets use same-filesystem ``os.replace``;
* every written file is verified by size and SHA-256;
* completed operations are rolled back in reverse order on failure;
* a committed receipt is written atomically for idempotent replay;
* artifact contents are never executed and no network calls are performed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .agent_contracts import canonical_json
from .agent_output_validation import ArtifactOperation
from .artifact_application_plan import (
    ArtifactApplicationDecision,
    ArtifactApplicationOperation,
    ArtifactApplicationPlan,
)
from .artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerificationResult,
    ArtifactPayloadVerificationStatus,
)
from .artifact_workspace_preflight import (
    ArtifactWorkspaceEntryType,
    ArtifactWorkspacePreflightResult,
    ArtifactWorkspacePreflightStatus,
)


ARTIFACT_TRANSACTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactTransactionError(RuntimeError):
    """Base class for transaction contract or execution failures."""


class ArtifactTransactionIntegrityError(ArtifactTransactionError):
    """A transaction object or durable receipt fails an integrity check."""


class ArtifactTransactionLockError(ArtifactTransactionError):
    """The exclusive transaction lock cannot be acquired safely."""


class ArtifactTransactionStatus(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled-back"
    FAILED = "failed"


class ArtifactTransactionOperationStatus(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled-back"
    FAILED = "failed"
    SKIPPED = "skipped"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactTransactionError(
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
        raise ArtifactTransactionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactTransactionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactTransactionError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactTransactionError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactTransactionError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactTransactionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactTransactionError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactTransactionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactTransactionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactTransactionError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactTransactionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _portable_relative_path(value: object, name: str) -> str:
    path = _text(value, name)
    if path != path.strip():
        raise ArtifactTransactionError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactTransactionError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactTransactionError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactTransactionError(
                f"{name} contains a non-portable segment"
            )
    return path


def _normalize_workspace_root(
    value: str | os.PathLike[str],
) -> str:
    path = Path(value).expanduser()
    if not path.exists():
        raise ArtifactTransactionError(
            "workspace_root must exist"
        )
    if path.is_symlink():
        raise ArtifactTransactionError(
            "workspace_root cannot be a symbolic link"
        )
    if not path.is_dir():
        raise ArtifactTransactionError(
            "workspace_root must be a directory"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactTransactionError(
            "workspace_root cannot be resolved"
        ) from exc
    return resolved.as_posix()


def _assert_inside_root(root: Path, candidate: Path) -> None:
    try:
        common = os.path.commonpath(
            [str(root), str(candidate.resolve(strict=False))]
        )
    except (OSError, ValueError) as exc:
        raise ArtifactTransactionError(
            "candidate path cannot be resolved safely"
        ) from exc
    if Path(common) != root:
        raise ArtifactTransactionError(
            "candidate path escapes workspace root"
        )


def _classify_lstat(path: Path) -> ArtifactWorkspaceEntryType:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ArtifactWorkspaceEntryType.ABSENT
    if stat.S_ISLNK(mode):
        return ArtifactWorkspaceEntryType.SYMLINK
    if stat.S_ISREG(mode):
        return ArtifactWorkspaceEntryType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return ArtifactWorkspaceEntryType.DIRECTORY
    return ArtifactWorkspaceEntryType.OTHER


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactTransactionError(
            f"cannot read file for hashing: {path}"
        ) from exc
    return digest.hexdigest()


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _existing_prefix_symlinks(
    root: Path,
    relative_path: str,
) -> tuple[str, ...]:
    current = root
    found: list[str] = []
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink():
            found.append(
                current.relative_to(root).as_posix()
            )
    return tuple(found)


def _case_conflicts(
    root: Path,
    relative_path: str,
) -> tuple[str, ...]:
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    parent = target.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        return ()
    try:
        entries = tuple(parent.iterdir())
    except OSError as exc:
        raise ArtifactTransactionError(
            f"cannot inspect directory entries: {parent}"
        ) from exc
    conflicts = [
        entry.name
        for entry in entries
        if (
            entry.name.casefold() == target.name.casefold()
            and entry.name != target.name
        )
    ]
    return tuple(sorted(conflicts))


def _safe_mkdir_chain(root: Path, directory: Path) -> None:
    _assert_inside_root(root, directory)
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            entry_type = _classify_lstat(current)
            if entry_type is ArtifactWorkspaceEntryType.SYMLINK:
                raise ArtifactTransactionError(
                    f"symbolic link detected in control path: {current}"
                )
            if entry_type is not ArtifactWorkspaceEntryType.DIRECTORY:
                raise ArtifactTransactionError(
                    f"non-directory detected in control path: {current}"
                )
            continue
        try:
            current.mkdir()
        except FileExistsError:
            entry_type = _classify_lstat(current)
            if entry_type is not ArtifactWorkspaceEntryType.DIRECTORY:
                raise ArtifactTransactionError(
                    f"control path was replaced concurrently: {current}"
                )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionPolicy:
    policy_id: str
    control_root: str = ".elman-os"
    lock_name: str = "transaction.lock"
    receipt_directory: str = "transactions"
    max_operations: int = 64
    max_total_payload_bytes: int = 50_000_000
    fsync_files: bool = True
    retain_backups_on_success: bool = True
    verify_after_write: bool = True
    version: int = ARTIFACT_TRANSACTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "control_root",
            _portable_relative_path(
                self.control_root,
                "control_root",
            ),
        )
        lock_name = _text(self.lock_name, "lock_name")
        if "/" in lock_name or "\\" in lock_name:
            raise ArtifactTransactionError(
                "lock_name must be a single portable segment"
            )
        if _PORTABLE_SEGMENT.fullmatch(lock_name) is None:
            raise ArtifactTransactionError(
                "lock_name is not portable"
            )
        object.__setattr__(self, "lock_name", lock_name)
        receipt_directory = _text(
            self.receipt_directory,
            "receipt_directory",
        )
        if "/" in receipt_directory or "\\" in receipt_directory:
            raise ArtifactTransactionError(
                "receipt_directory must be a single portable segment"
            )
        if _PORTABLE_SEGMENT.fullmatch(receipt_directory) is None:
            raise ArtifactTransactionError(
                "receipt_directory is not portable"
            )
        object.__setattr__(
            self,
            "receipt_directory",
            receipt_directory,
        )
        object.__setattr__(
            self,
            "max_operations",
            _positive_int(
                self.max_operations,
                "max_operations",
            ),
        )
        object.__setattr__(
            self,
            "max_total_payload_bytes",
            _positive_int(
                self.max_total_payload_bytes,
                "max_total_payload_bytes",
            ),
        )
        for field_name in (
            "fsync_files",
            "retain_backups_on_success",
            "verify_after_write",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        if self.version != ARTIFACT_TRANSACTION_FORMAT_VERSION:
            raise ArtifactTransactionError(
                "unsupported transaction format version"
            )

    @property
    def lock_relative_path(self) -> str:
        return f"{self.control_root}/{self.lock_name}"

    @property
    def receipt_root(self) -> str:
        return f"{self.control_root}/{self.receipt_directory}"

    def receipt_relative_path(self, transaction_id: str) -> str:
        normalized = _identifier(
            transaction_id,
            "transaction_id",
        )
        safe_name = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        return f"{self.receipt_root}/{safe_name}.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "control_root": self.control_root,
            "lock_name": self.lock_name,
            "receipt_directory": self.receipt_directory,
            "max_operations": self.max_operations,
            "max_total_payload_bytes": (
                self.max_total_payload_bytes
            ),
            "fsync_files": self.fsync_files,
            "retain_backups_on_success": (
                self.retain_backups_on_success
            ),
            "verify_after_write": self.verify_after_write,
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
    ) -> "ArtifactTransactionPolicy":
        if data.get("record_type") != "artifact_transaction_policy":
            raise ArtifactTransactionError(
                "record_type must be artifact_transaction_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            control_root=data["control_root"],
            lock_name=data["lock_name"],
            receipt_directory=data["receipt_directory"],
            max_operations=data["max_operations"],
            max_total_payload_bytes=data[
                "max_total_payload_bytes"
            ],
            fsync_files=data["fsync_files"],
            retain_backups_on_success=data[
                "retain_backups_on_success"
            ],
            verify_after_write=data["verify_after_write"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionError(
                "transaction policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionError(
                "transaction policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionRequest:
    transaction_id: str
    policy_id: str
    policy_hash: str
    preflight_id: str
    preflight_result_hash: str
    snapshot_hash: str
    verification_id: str
    verification_result_hash: str
    payload_manifest_hash: str
    application_id: str
    application_plan_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "transaction_id",
            "policy_id",
            "preflight_id",
            "verification_id",
            "application_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        for field_name in (
            "policy_hash",
            "preflight_result_hash",
            "snapshot_hash",
            "verification_result_hash",
            "payload_manifest_hash",
            "application_plan_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(
                    getattr(self, field_name),
                    field_name,
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
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        workspace_root = _text(
            self.workspace_root,
            "workspace_root",
        )
        if not Path(workspace_root).is_absolute():
            raise ArtifactTransactionError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(workspace_root).as_posix(),
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(
                self.requested_by,
                "requested_by",
                _AGENT_ID,
            ),
        )
        object.__setattr__(
            self,
            "requested_at",
            _utc_timestamp(
                self.requested_at,
                "requested_at",
            ),
        )
        if self.version != ARTIFACT_TRANSACTION_FORMAT_VERSION:
            raise ArtifactTransactionError(
                "unsupported transaction format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactTransactionIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        application_plan: ArtifactApplicationPlan,
        verification_result: ArtifactPayloadVerificationResult,
        preflight_result: ArtifactWorkspacePreflightResult,
        policy: ArtifactTransactionPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        transaction_id: str | None = None,
    ) -> "ArtifactTransactionRequest":
        if not isinstance(application_plan, ArtifactApplicationPlan):
            raise ArtifactTransactionError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactTransactionError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(
            preflight_result,
            ArtifactWorkspacePreflightResult,
        ):
            raise ArtifactTransactionError(
                "preflight_result must be an ArtifactWorkspacePreflightResult"
            )
        if not isinstance(policy, ArtifactTransactionPolicy):
            raise ArtifactTransactionError(
                "policy must be an ArtifactTransactionPolicy"
            )
        application_plan.verify_hash()
        verification_result.verify_hash()
        preflight_result.verify_hash()
        plan_hash = application_plan.plan_hash
        verification_hash = verification_result.result_hash
        preflight_hash = preflight_result.result_hash
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None

        normalized_root = _normalize_workspace_root(
            preflight_result.workspace_root
        )
        normalized_requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_time = _utc_timestamp(
            requested_at,
            "requested_at",
        )

        identity_hash = _sha256_document(
            {
                "record_type": "artifact_transaction_identity",
                "policy_hash": policy.policy_hash,
                "preflight_result_hash": preflight_hash,
                "snapshot_hash": preflight_result.snapshot_hash,
                "verification_result_hash": verification_hash,
                "payload_manifest_hash": (
                    verification_result.payload_manifest_hash
                ),
                "application_plan_hash": plan_hash,
                "workspace_root": normalized_root,
            }
        )
        effective_id = (
            transaction_id
            if transaction_id is not None
            else f"artifact-transaction:{identity_hash}"
        )

        return cls(
            transaction_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            preflight_id=preflight_result.preflight_id,
            preflight_result_hash=preflight_hash,
            snapshot_hash=preflight_result.snapshot_hash,
            verification_id=verification_result.verification_id,
            verification_result_hash=verification_hash,
            payload_manifest_hash=(
                verification_result.payload_manifest_hash
            ),
            application_id=application_plan.application_id,
            application_plan_hash=plan_hash,
            plan_id=application_plan.plan_id,
            step_id=application_plan.step_id,
            agent_id=application_plan.agent_id,
            workspace_root=normalized_root,
            requested_by=normalized_requester,
            requested_at=normalized_time,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_request",
            "version": self.version,
            "transaction_id": self.transaction_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "preflight_id": self.preflight_id,
            "preflight_result_hash": self.preflight_result_hash,
            "snapshot_hash": self.snapshot_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": (
                self.verification_result_hash
            ),
            "payload_manifest_hash": self.payload_manifest_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactTransactionIntegrityError(
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
    ) -> "ArtifactTransactionRequest":
        if data.get("record_type") != "artifact_transaction_request":
            raise ArtifactTransactionError(
                "record_type must be artifact_transaction_request"
            )
        if "request_hash" not in data:
            raise ArtifactTransactionIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            transaction_id=data["transaction_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            preflight_id=data["preflight_id"],
            preflight_result_hash=data["preflight_result_hash"],
            snapshot_hash=data["snapshot_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data[
                "verification_result_hash"
            ],
            payload_manifest_hash=data[
                "payload_manifest_hash"
            ],
            application_id=data["application_id"],
            application_plan_hash=data[
                "application_plan_hash"
            ],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            workspace_root=data["workspace_root"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionError(
                "transaction request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionError(
                "transaction request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionOperationResult:
    sequence: int
    operation_id: str
    destination_path: str
    operation: ArtifactOperation
    status: ArtifactTransactionOperationStatus
    payload_sha256: str
    before_sha256: str | None
    after_sha256: str | None
    backup_path: str | None
    bytes_written: int
    reason: str
    operation_result_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sequence",
            _positive_int(self.sequence, "sequence"),
        )
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        object.__setattr__(
            self,
            "destination_path",
            _portable_relative_path(
                self.destination_path,
                "destination_path",
            ),
        )
        try:
            operation = ArtifactOperation(self.operation)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionError(
                "operation is invalid"
            ) from exc
        object.__setattr__(self, "operation", operation)
        try:
            status_value = ArtifactTransactionOperationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionError(
                "operation status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        object.__setattr__(
            self,
            "payload_sha256",
            _hash(self.payload_sha256, "payload_sha256"),
        )
        for field_name in ("before_sha256", "after_sha256"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _hash(value, field_name),
                )
        if self.backup_path is not None:
            object.__setattr__(
                self,
                "backup_path",
                _portable_relative_path(
                    self.backup_path,
                    "backup_path",
                ),
            )
        object.__setattr__(
            self,
            "bytes_written",
            _non_negative_int(
                self.bytes_written,
                "bytes_written",
            ),
        )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        computed = self.compute_hash()
        if self.operation_result_hash is None:
            object.__setattr__(
                self,
                "operation_result_hash",
                computed,
            )
        else:
            supplied = _hash(
                self.operation_result_hash,
                "operation_result_hash",
            )
            if supplied != computed:
                raise ArtifactTransactionIntegrityError(
                    "operation result hash does not match content"
                )
            object.__setattr__(
                self,
                "operation_result_hash",
                supplied,
            )

    def hash_material(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "operation": self.operation.value,
            "status": self.status.value,
            "payload_sha256": self.payload_sha256,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "backup_path": self.backup_path,
            "bytes_written": self.bytes_written,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.operation_result_hash != self.compute_hash():
            raise ArtifactTransactionIntegrityError(
                "operation result hash does not match content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["operation_result_hash"] = (
            self.operation_result_hash
        )
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionOperationResult":
        return cls(
            sequence=data["sequence"],
            operation_id=data["operation_id"],
            destination_path=data["destination_path"],
            operation=ArtifactOperation(data["operation"]),
            status=ArtifactTransactionOperationStatus(
                data["status"]
            ),
            payload_sha256=data["payload_sha256"],
            before_sha256=data.get("before_sha256"),
            after_sha256=data.get("after_sha256"),
            backup_path=data.get("backup_path"),
            bytes_written=data["bytes_written"],
            reason=data["reason"],
            operation_result_hash=data.get(
                "operation_result_hash"
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionResult:
    transaction_id: str
    status: ArtifactTransactionStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    preflight_id: str
    preflight_result_hash: str
    snapshot_hash: str
    verification_id: str
    verification_result_hash: str
    payload_manifest_hash: str
    application_id: str
    application_plan_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    lock_path: str
    receipt_path: str
    operations: tuple[ArtifactTransactionOperationResult, ...]
    committed_count: int
    rolled_back_count: int
    failed_count: int
    started_at: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "transaction_id",
            "policy_id",
            "preflight_id",
            "verification_id",
            "application_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        try:
            status_value = ArtifactTransactionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionError(
                "transaction status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        for field_name in (
            "request_hash",
            "policy_hash",
            "preflight_result_hash",
            "snapshot_hash",
            "verification_result_hash",
            "payload_manifest_hash",
            "application_plan_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(
                    getattr(self, field_name),
                    field_name,
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
            "agent_id",
            _identifier(self.agent_id, "agent_id", _AGENT_ID),
        )
        workspace_root = _text(
            self.workspace_root,
            "workspace_root",
        )
        if not Path(workspace_root).is_absolute():
            raise ArtifactTransactionError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(workspace_root).as_posix(),
        )
        object.__setattr__(
            self,
            "lock_path",
            _portable_relative_path(
                self.lock_path,
                "lock_path",
            ),
        )
        object.__setattr__(
            self,
            "receipt_path",
            _portable_relative_path(
                self.receipt_path,
                "receipt_path",
            ),
        )
        operations = tuple(self.operations)
        if not all(
            isinstance(item, ArtifactTransactionOperationResult)
            for item in operations
        ):
            raise ArtifactTransactionError(
                "operations must contain transaction operation results"
            )
        if tuple(item.sequence for item in operations) != tuple(
            range(1, len(operations) + 1)
        ):
            raise ArtifactTransactionError(
                "operation result sequences must be contiguous from one"
            )
        for item in operations:
            item.verify_hash()
        object.__setattr__(self, "operations", operations)
        for field_name in (
            "committed_count",
            "rolled_back_count",
            "failed_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        actual_committed = sum(
            item.status
            is ArtifactTransactionOperationStatus.COMMITTED
            for item in operations
        )
        actual_rolled_back = sum(
            item.status
            is ArtifactTransactionOperationStatus.ROLLED_BACK
            for item in operations
        )
        actual_failed = sum(
            item.status
            is ArtifactTransactionOperationStatus.FAILED
            for item in operations
        )
        if (
            self.committed_count,
            self.rolled_back_count,
            self.failed_count,
        ) != (
            actual_committed,
            actual_rolled_back,
            actual_failed,
        ):
            raise ArtifactTransactionIntegrityError(
                "transaction counts do not match operation results"
            )
        if status_value is ArtifactTransactionStatus.COMMITTED:
            if operations and actual_committed != len(operations):
                raise ArtifactTransactionIntegrityError(
                    "committed transaction must contain only committed operations"
                )
            if actual_failed or actual_rolled_back:
                raise ArtifactTransactionIntegrityError(
                    "committed transaction cannot contain rollback or failure"
                )
        elif status_value is ArtifactTransactionStatus.ROLLED_BACK:
            if actual_committed:
                raise ArtifactTransactionIntegrityError(
                    "rolled-back transaction cannot retain committed operations"
                )
            if not actual_rolled_back and operations:
                raise ArtifactTransactionIntegrityError(
                    "rolled-back transaction requires rolled-back operations"
                )
        object.__setattr__(
            self,
            "started_at",
            _utc_timestamp(self.started_at, "started_at"),
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
        if self.version != ARTIFACT_TRANSACTION_FORMAT_VERSION:
            raise ArtifactTransactionError(
                "unsupported transaction format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactTransactionIntegrityError(
                    "transaction result hash does not match content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_result",
            "version": self.version,
            "transaction_id": self.transaction_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "preflight_id": self.preflight_id,
            "preflight_result_hash": self.preflight_result_hash,
            "snapshot_hash": self.snapshot_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": (
                self.verification_result_hash
            ),
            "payload_manifest_hash": self.payload_manifest_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "lock_path": self.lock_path,
            "receipt_path": self.receipt_path,
            "operations": [
                item.to_dict()
                for item in self.operations
            ],
            "committed_count": self.committed_count,
            "rolled_back_count": self.rolled_back_count,
            "failed_count": self.failed_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactTransactionIntegrityError(
                "transaction result hash does not match content"
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
    ) -> "ArtifactTransactionResult":
        if data.get("record_type") != "artifact_transaction_result":
            raise ArtifactTransactionError(
                "record_type must be artifact_transaction_result"
            )
        if "result_hash" not in data:
            raise ArtifactTransactionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            transaction_id=data["transaction_id"],
            status=ArtifactTransactionStatus(data["status"]),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            preflight_id=data["preflight_id"],
            preflight_result_hash=data[
                "preflight_result_hash"
            ],
            snapshot_hash=data["snapshot_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data[
                "verification_result_hash"
            ],
            payload_manifest_hash=data[
                "payload_manifest_hash"
            ],
            application_id=data["application_id"],
            application_plan_hash=data[
                "application_plan_hash"
            ],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            workspace_root=data["workspace_root"],
            lock_path=data["lock_path"],
            receipt_path=data["receipt_path"],
            operations=tuple(
                ArtifactTransactionOperationResult.from_dict(
                    item
                )
                for item in data["operations"]
            ),
            committed_count=data["committed_count"],
            rolled_back_count=data["rolled_back_count"],
            failed_count=data["failed_count"],
            started_at=data["started_at"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionIntegrityError(
                "transaction result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionIntegrityError(
                "transaction result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(slots=True)
class _AppliedOperation:
    operation: ArtifactApplicationOperation
    payload: ArtifactPayload
    before_sha256: str | None
    backup_path: Path | None


@dataclass(frozen=True, slots=True)
class ArtifactTransactionApplication:
    request: ArtifactTransactionRequest
    application_plan: ArtifactApplicationPlan
    verification_result: ArtifactPayloadVerificationResult
    preflight_result: ArtifactWorkspacePreflightResult
    policy: ArtifactTransactionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.request, ArtifactTransactionRequest):
            raise ArtifactTransactionError(
                "request must be an ArtifactTransactionRequest"
            )
        if not isinstance(
            self.application_plan,
            ArtifactApplicationPlan,
        ):
            raise ArtifactTransactionError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            self.verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactTransactionError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(
            self.preflight_result,
            ArtifactWorkspacePreflightResult,
        ):
            raise ArtifactTransactionError(
                "preflight_result must be an ArtifactWorkspacePreflightResult"
            )
        if not isinstance(self.policy, ArtifactTransactionPolicy):
            raise ArtifactTransactionError(
                "policy must be an ArtifactTransactionPolicy"
            )

        self.request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()
        self.preflight_result.verify_hash()

        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        preflight_hash = self.preflight_result.result_hash
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None

        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "preflight_id": self.preflight_result.preflight_id,
            "preflight_result_hash": preflight_hash,
            "snapshot_hash": self.preflight_result.snapshot_hash,
            "verification_id": (
                self.verification_result.verification_id
            ),
            "verification_result_hash": verification_hash,
            "payload_manifest_hash": (
                self.verification_result.payload_manifest_hash
            ),
            "application_id": self.application_plan.application_id,
            "application_plan_hash": plan_hash,
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
            "workspace_root": self.preflight_result.workspace_root,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactTransactionError(
                    f"request {field_name} does not match transaction source"
                )

        if (
            self.application_plan.decision
            is not ArtifactApplicationDecision.READY
        ):
            raise ArtifactTransactionError(
                "application plan decision must be ready"
            )
        if (
            self.verification_result.status
            is not ArtifactPayloadVerificationStatus.VERIFIED
        ):
            raise ArtifactTransactionError(
                "payload verification status must be verified"
            )
        if (
            self.preflight_result.status
            is not ArtifactWorkspacePreflightStatus.READY
        ):
            raise ArtifactTransactionError(
                "workspace preflight status must be ready"
            )
        if not self.application_plan.operations:
            raise ArtifactTransactionError(
                "application plan contains no operations"
            )
        if (
            len(self.application_plan.operations)
            > self.policy.max_operations
        ):
            raise ArtifactTransactionError(
                "operation count exceeds transaction policy maximum"
            )
        if (
            self.verification_result.payload_total_bytes
            > self.policy.max_total_payload_bytes
        ):
            raise ArtifactTransactionError(
                "payload bytes exceed transaction policy maximum"
            )
        if len(self.application_plan.operations) != len(
            self.verification_result.payloads
        ):
            raise ArtifactTransactionError(
                "operation and payload counts do not match"
            )
        if len(self.application_plan.operations) != len(
            self.preflight_result.snapshot
        ):
            raise ArtifactTransactionError(
                "operation and preflight snapshot counts do not match"
            )

        control_prefix = self.policy.control_root.casefold() + "/"
        for operation in self.application_plan.operations:
            candidate = operation.destination_path.casefold()
            if (
                candidate == self.policy.control_root.casefold()
                or candidate.startswith(control_prefix)
            ):
                raise ArtifactTransactionError(
                    "artifact destination overlaps transaction control root"
                )

    @property
    def _root(self) -> Path:
        return Path(self.request.workspace_root)

    @property
    def _lock_path(self) -> Path:
        return self._root.joinpath(
            *PurePosixPath(
                self.policy.lock_relative_path
            ).parts
        )

    @property
    def _receipt_relative_path(self) -> str:
        return self.policy.receipt_relative_path(
            self.request.transaction_id
        )

    @property
    def _receipt_path(self) -> Path:
        return self._root.joinpath(
            *PurePosixPath(
                self._receipt_relative_path
            ).parts
        )

    def apply(self) -> ArtifactTransactionResult:
        """Apply all artifacts transactionally.

        A committed receipt makes replay idempotent: when the same request is
        executed again and every final file still matches its payload, the
        existing verified result is returned without acquiring the lock or
        rewriting any file.
        """

        existing = self._load_committed_receipt()
        if existing is not None:
            self._verify_committed_destinations(existing)
            return existing

        root_text = _normalize_workspace_root(
            self.request.workspace_root
        )
        if root_text != self.request.workspace_root:
            raise ArtifactTransactionError(
                "workspace root changed after transaction request creation"
            )

        self._acquire_lock()
        started_at = self.request.requested_at
        applied: list[_AppliedOperation] = []
        successful_results: list[
            ArtifactTransactionOperationResult
        ] = []
        try:
            self._revalidate_snapshot()

            payloads_by_id = {
                payload.operation_id: payload
                for payload in self.verification_result.payloads
            }
            snapshots_by_id = {
                entry.operation_id: entry
                for entry in self.preflight_result.snapshot
            }

            for operation in self.application_plan.operations:
                payload = payloads_by_id.get(operation.operation_id)
                snapshot = snapshots_by_id.get(operation.operation_id)
                if payload is None or snapshot is None:
                    raise ArtifactTransactionIntegrityError(
                        "operation boundary is incomplete"
                    )
                before_sha256 = snapshot.existing_sha256
                backup_path: Path | None = None
                if operation.operation is ArtifactOperation.UPDATE:
                    backup_path = self._create_backup(
                        operation,
                        snapshot.existing_sha256,
                        snapshot.existing_size_bytes,
                    )
                self._write_operation(operation, payload)
                after_sha256 = _file_sha256(
                    self._destination_path(operation)
                )
                if (
                    self.policy.verify_after_write
                    and (
                        after_sha256 != operation.sha256
                        or self._destination_path(
                            operation
                        ).stat().st_size
                        != operation.size_bytes
                    )
                ):
                    raise ArtifactTransactionIntegrityError(
                        "post-write verification failed"
                    )
                applied.append(
                    _AppliedOperation(
                        operation=operation,
                        payload=payload,
                        before_sha256=before_sha256,
                        backup_path=backup_path,
                    )
                )
                successful_results.append(
                    ArtifactTransactionOperationResult(
                        sequence=operation.sequence,
                        operation_id=operation.operation_id,
                        destination_path=operation.destination_path,
                        operation=operation.operation,
                        status=(
                            ArtifactTransactionOperationStatus.COMMITTED
                        ),
                        payload_sha256=payload.content_sha256,
                        before_sha256=before_sha256,
                        after_sha256=after_sha256,
                        backup_path=operation.backup_path,
                        bytes_written=payload.size_bytes,
                        reason=(
                            "COMMITTED: artifact was applied and verified"
                        ),
                    )
                )

            result = self._build_result(
                status=ArtifactTransactionStatus.COMMITTED,
                operations=tuple(successful_results),
                started_at=started_at,
                completed_at=started_at,
                reason=(
                    "COMMITTED: all artifact operations completed atomically "
                    "with verified final content"
                ),
            )
            self._write_receipt(result)
            if not self.policy.retain_backups_on_success:
                try:
                    self._delete_retained_backups(applied)
                except ArtifactTransactionError:
                    # Backup cleanup is post-commit housekeeping. A durable
                    # committed receipt must never be invalidated by cleanup.
                    pass
            return result
        except Exception as exc:
            rollback_results, rollback_failed = self._rollback(
                applied,
                exc,
            )
            status_value = (
                ArtifactTransactionStatus.FAILED
                if rollback_failed or not applied
                else ArtifactTransactionStatus.ROLLED_BACK
            )
            combined = self._combine_failure_results(
                successful_results,
                rollback_results,
                failed_reason=str(exc),
            )
            return self._build_result(
                status=status_value,
                operations=combined,
                started_at=started_at,
                completed_at=started_at,
                reason=(
                    f"{status_value.value.upper()}: transaction failed: {exc}"
                ),
            )
        finally:
            self._release_lock()

    def _destination_path(
        self,
        operation: ArtifactApplicationOperation,
    ) -> Path:
        destination = self._root.joinpath(
            *PurePosixPath(operation.destination_path).parts
        )
        _assert_inside_root(self._root, destination)
        return destination

    def _backup_path(
        self,
        operation: ArtifactApplicationOperation,
    ) -> Path:
        if operation.backup_path is None:
            raise ArtifactTransactionIntegrityError(
                "update operation is missing backup_path"
            )
        backup = self._root.joinpath(
            *PurePosixPath(operation.backup_path).parts
        )
        _assert_inside_root(self._root, backup)
        return backup

    def _acquire_lock(self) -> None:
        lock_parent = self._lock_path.parent
        _safe_mkdir_chain(self._root, lock_parent)
        _assert_inside_root(self._root, self._lock_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(
                self._lock_path,
                flags,
                0o600,
            )
        except FileExistsError as exc:
            raise ArtifactTransactionLockError(
                f"transaction lock already exists: {self.policy.lock_relative_path}"
            ) from exc
        except OSError as exc:
            raise ArtifactTransactionLockError(
                "transaction lock cannot be created"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    canonical_json(
                        {
                            "transaction_id": (
                                self.request.transaction_id
                            ),
                            "request_hash": self.request.request_hash,
                        }
                    )
                )
                if self.policy.fsync_files:
                    _fsync_file(handle)
        except Exception:
            try:
                self._lock_path.unlink()
            except OSError:
                pass
            raise

    def _release_lock(self) -> None:
        try:
            if self._lock_path.exists() or self._lock_path.is_symlink():
                if self._lock_path.is_symlink():
                    raise ArtifactTransactionLockError(
                        "transaction lock path became a symbolic link"
                    )
                self._lock_path.unlink()
        except FileNotFoundError:
            return

    def _revalidate_snapshot(self) -> None:
        root = self._root
        if root.is_symlink():
            raise ArtifactTransactionError(
                "workspace root became a symbolic link"
            )
        for operation, snapshot in zip(
            self.application_plan.operations,
            self.preflight_result.snapshot,
            strict=True,
        ):
            if (
                operation.sequence != snapshot.sequence
                or operation.operation_id != snapshot.operation_id
                or operation.destination_path
                != snapshot.destination_path
            ):
                raise ArtifactTransactionIntegrityError(
                    "preflight snapshot does not align with application operation"
                )
            destination = self._destination_path(operation)
            if _existing_prefix_symlinks(
                root,
                operation.destination_path,
            ):
                raise ArtifactTransactionError(
                    "symbolic link detected during transaction revalidation"
                )
            if _case_conflicts(
                root,
                operation.destination_path,
            ):
                raise ArtifactTransactionError(
                    "case-conflicting destination appeared after preflight"
                )
            parent = destination.parent
            if (
                not parent.exists()
                or parent.is_symlink()
                or not parent.is_dir()
                or not os.access(parent, os.W_OK)
            ):
                raise ArtifactTransactionError(
                    "destination parent is no longer an existing writable directory"
                )

            entry_type = _classify_lstat(destination)
            if operation.operation is ArtifactOperation.CREATE:
                if entry_type is not ArtifactWorkspaceEntryType.ABSENT:
                    raise ArtifactTransactionError(
                        "create destination changed after preflight"
                    )
            else:
                if (
                    entry_type
                    is not ArtifactWorkspaceEntryType.REGULAR_FILE
                ):
                    raise ArtifactTransactionError(
                        "update destination changed type after preflight"
                    )
                current_size = destination.stat().st_size
                current_hash = _file_sha256(destination)
                if (
                    current_size != snapshot.existing_size_bytes
                    or current_hash != snapshot.existing_sha256
                ):
                    raise ArtifactTransactionError(
                        "update destination content changed after preflight"
                    )
                backup = self._backup_path(operation)
                if backup.exists() or backup.is_symlink():
                    raise ArtifactTransactionError(
                        "backup destination already exists"
                    )
                if _existing_prefix_symlinks(
                    root,
                    operation.backup_path or "",
                ):
                    raise ArtifactTransactionError(
                        "symbolic link detected in backup path"
                    )

    def _create_backup(
        self,
        operation: ArtifactApplicationOperation,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> Path:
        destination = self._destination_path(operation)
        backup = self._backup_path(operation)
        _safe_mkdir_chain(self._root, backup.parent)
        if backup.exists() or backup.is_symlink():
            raise ArtifactTransactionError(
                "backup destination already exists"
            )
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".elman-backup-",
            suffix=".tmp",
            dir=backup.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                with destination.open("rb") as source:
                    shutil.copyfileobj(
                        source,
                        target,
                        length=1024 * 1024,
                    )
                if self.policy.fsync_files:
                    _fsync_file(target)
            if (
                expected_size is None
                or expected_sha256 is None
                or temp_path.stat().st_size != expected_size
                or _file_sha256(temp_path) != expected_sha256
            ):
                raise ArtifactTransactionIntegrityError(
                    "backup verification failed"
                )
            os.link(temp_path, backup)
            temp_path.unlink()
            return backup
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            raise

    def _write_operation(
        self,
        operation: ArtifactApplicationOperation,
        payload: ArtifactPayload,
    ) -> None:
        destination = self._destination_path(operation)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".elman-write-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload.content)
                if self.policy.fsync_files:
                    _fsync_file(handle)
            if (
                temp_path.stat().st_size != operation.size_bytes
                or _file_sha256(temp_path) != operation.sha256
            ):
                raise ArtifactTransactionIntegrityError(
                    "temporary payload verification failed"
                )
            if operation.operation is ArtifactOperation.CREATE:
                self._commit_create(temp_path, destination)
            else:
                self._commit_update(temp_path, destination)
        finally:
            try:
                if temp_path.exists() or temp_path.is_symlink():
                    temp_path.unlink()
            except OSError:
                pass

    def _commit_create(
        self,
        temp_path: Path,
        destination: Path,
    ) -> None:
        try:
            os.link(temp_path, destination)
        except FileExistsError as exc:
            raise ArtifactTransactionError(
                "create destination appeared during commit"
            ) from exc
        except OSError as exc:
            raise ArtifactTransactionError(
                "atomic no-overwrite create commit failed"
            ) from exc
        temp_path.unlink()

    def _commit_update(
        self,
        temp_path: Path,
        destination: Path,
    ) -> None:
        try:
            os.replace(temp_path, destination)
        except OSError as exc:
            raise ArtifactTransactionError(
                "atomic update replacement failed"
            ) from exc

    def _rollback(
        self,
        applied: list[_AppliedOperation],
        cause: Exception,
    ) -> tuple[
        tuple[ArtifactTransactionOperationResult, ...],
        bool,
    ]:
        reversed_results: list[
            ArtifactTransactionOperationResult
        ] = []
        rollback_failed = False
        for item in reversed(applied):
            operation = item.operation
            destination = self._destination_path(operation)
            try:
                current_type = _classify_lstat(destination)
                if (
                    current_type
                    is not ArtifactWorkspaceEntryType.REGULAR_FILE
                ):
                    raise ArtifactTransactionIntegrityError(
                        "rollback destination is not a regular file"
                    )
                current_hash = _file_sha256(destination)
                if current_hash != item.payload.content_sha256:
                    raise ArtifactTransactionIntegrityError(
                        "rollback refused because destination changed after write"
                    )
                if operation.operation is ArtifactOperation.CREATE:
                    destination.unlink()
                else:
                    if item.backup_path is None:
                        raise ArtifactTransactionIntegrityError(
                            "rollback backup is unavailable"
                        )
                    backup_type = _classify_lstat(item.backup_path)
                    if (
                        backup_type
                        is not ArtifactWorkspaceEntryType.REGULAR_FILE
                    ):
                        raise ArtifactTransactionIntegrityError(
                            "rollback backup is not a regular file"
                        )
                    if (
                        item.before_sha256 is None
                        or _file_sha256(item.backup_path)
                        != item.before_sha256
                    ):
                        raise ArtifactTransactionIntegrityError(
                            "rollback backup hash changed"
                        )
                    os.replace(item.backup_path, destination)
                    if _file_sha256(destination) != item.before_sha256:
                        raise ArtifactTransactionIntegrityError(
                            "rollback restore verification failed"
                        )
                reversed_results.append(
                    ArtifactTransactionOperationResult(
                        sequence=operation.sequence,
                        operation_id=operation.operation_id,
                        destination_path=operation.destination_path,
                        operation=operation.operation,
                        status=(
                            ArtifactTransactionOperationStatus.ROLLED_BACK
                        ),
                        payload_sha256=item.payload.content_sha256,
                        before_sha256=item.before_sha256,
                        after_sha256=(
                            item.before_sha256
                            if operation.operation
                            is ArtifactOperation.UPDATE
                            else None
                        ),
                        backup_path=operation.backup_path,
                        bytes_written=item.payload.size_bytes,
                        reason=(
                            "ROLLED-BACK: operation was reversed after "
                            f"transaction failure: {cause}"
                        ),
                    )
                )
            except Exception as rollback_exc:
                rollback_failed = True
                reversed_results.append(
                    ArtifactTransactionOperationResult(
                        sequence=operation.sequence,
                        operation_id=operation.operation_id,
                        destination_path=operation.destination_path,
                        operation=operation.operation,
                        status=(
                            ArtifactTransactionOperationStatus.FAILED
                        ),
                        payload_sha256=item.payload.content_sha256,
                        before_sha256=item.before_sha256,
                        after_sha256=(
                            _file_sha256(destination)
                            if _classify_lstat(destination)
                            is ArtifactWorkspaceEntryType.REGULAR_FILE
                            else None
                        ),
                        backup_path=operation.backup_path,
                        bytes_written=item.payload.size_bytes,
                        reason=(
                            "FAILED: rollback could not be completed: "
                            f"{rollback_exc}"
                        ),
                    )
                )
        ordered = tuple(
            sorted(
                reversed_results,
                key=lambda result: result.sequence,
            )
        )
        return ordered, rollback_failed

    def _combine_failure_results(
        self,
        successful_results: list[
            ArtifactTransactionOperationResult
        ],
        rollback_results: tuple[
            ArtifactTransactionOperationResult,
            ...,
        ],
        *,
        failed_reason: str,
    ) -> tuple[ArtifactTransactionOperationResult, ...]:
        by_sequence = {
            item.sequence: item
            for item in rollback_results
        }
        payloads_by_id = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        snapshots_by_id = {
            snapshot.operation_id: snapshot
            for snapshot in self.preflight_result.snapshot
        }
        combined: list[ArtifactTransactionOperationResult] = []
        successful_sequences = {
            item.sequence
            for item in successful_results
        }
        failure_assigned = False
        for operation in self.application_plan.operations:
            replacement = by_sequence.get(operation.sequence)
            if replacement is not None:
                combined.append(replacement)
                continue
            payload = payloads_by_id[operation.operation_id]
            snapshot = snapshots_by_id[operation.operation_id]
            if operation.sequence in successful_sequences:
                combined.append(
                    ArtifactTransactionOperationResult(
                        sequence=operation.sequence,
                        operation_id=operation.operation_id,
                        destination_path=operation.destination_path,
                        operation=operation.operation,
                        status=(
                            ArtifactTransactionOperationStatus.FAILED
                        ),
                        payload_sha256=payload.content_sha256,
                        before_sha256=snapshot.existing_sha256,
                        after_sha256=(
                            _file_sha256(
                                self._destination_path(operation)
                            )
                            if _classify_lstat(
                                self._destination_path(operation)
                            )
                            is ArtifactWorkspaceEntryType.REGULAR_FILE
                            else None
                        ),
                        backup_path=operation.backup_path,
                        bytes_written=payload.size_bytes,
                        reason=(
                            "FAILED: operation committed but rollback result "
                            "was unavailable"
                        ),
                    )
                )
                continue
            if not failure_assigned:
                status_value = (
                    ArtifactTransactionOperationStatus.FAILED
                )
                reason = (
                    "FAILED: operation could not be committed: "
                    f"{failed_reason}"
                )
                failure_assigned = True
            else:
                status_value = (
                    ArtifactTransactionOperationStatus.SKIPPED
                )
                reason = (
                    "SKIPPED: earlier transaction operation failed"
                )
            combined.append(
                ArtifactTransactionOperationResult(
                    sequence=operation.sequence,
                    operation_id=operation.operation_id,
                    destination_path=operation.destination_path,
                    operation=operation.operation,
                    status=status_value,
                    payload_sha256=payload.content_sha256,
                    before_sha256=snapshot.existing_sha256,
                    after_sha256=None,
                    backup_path=operation.backup_path,
                    bytes_written=0,
                    reason=reason,
                )
            )
        return tuple(combined)

    def _build_result(
        self,
        *,
        status: ArtifactTransactionStatus,
        operations: tuple[
            ArtifactTransactionOperationResult,
            ...,
        ],
        started_at: str,
        completed_at: str,
        reason: str,
    ) -> ArtifactTransactionResult:
        request_hash = self.request.request_hash
        assert request_hash is not None
        return ArtifactTransactionResult(
            transaction_id=self.request.transaction_id,
            status=status,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            preflight_id=self.preflight_result.preflight_id,
            preflight_result_hash=(
                self.request.preflight_result_hash
            ),
            snapshot_hash=self.preflight_result.snapshot_hash,
            verification_id=(
                self.verification_result.verification_id
            ),
            verification_result_hash=(
                self.request.verification_result_hash
            ),
            payload_manifest_hash=(
                self.verification_result.payload_manifest_hash
            ),
            application_id=self.application_plan.application_id,
            application_plan_hash=(
                self.request.application_plan_hash
            ),
            plan_id=self.application_plan.plan_id,
            step_id=self.application_plan.step_id,
            agent_id=self.application_plan.agent_id,
            workspace_root=self.request.workspace_root,
            lock_path=self.policy.lock_relative_path,
            receipt_path=self._receipt_relative_path,
            operations=operations,
            committed_count=sum(
                item.status
                is ArtifactTransactionOperationStatus.COMMITTED
                for item in operations
            ),
            rolled_back_count=sum(
                item.status
                is ArtifactTransactionOperationStatus.ROLLED_BACK
                for item in operations
            ),
            failed_count=sum(
                item.status
                is ArtifactTransactionOperationStatus.FAILED
                for item in operations
            ),
            started_at=started_at,
            completed_at=completed_at,
            reason=reason,
        )

    def _write_receipt(
        self,
        result: ArtifactTransactionResult,
    ) -> None:
        if result.status is not ArtifactTransactionStatus.COMMITTED:
            raise ArtifactTransactionIntegrityError(
                "only committed results may be written as durable receipts"
            )
        _safe_mkdir_chain(self._root, self._receipt_path.parent)
        if self._receipt_path.exists() or self._receipt_path.is_symlink():
            raise ArtifactTransactionIntegrityError(
                "transaction receipt appeared during commit"
            )
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".elman-receipt-",
            suffix=".tmp",
            dir=self._receipt_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(result.to_json())
                handle.write("\n")
                if self.policy.fsync_files:
                    _fsync_file(handle)
            os.link(temp_path, self._receipt_path)
            temp_path.unlink()
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            try:
                if self._receipt_path.exists():
                    self._receipt_path.unlink()
            except OSError:
                pass
            raise

    def _load_committed_receipt(
        self,
    ) -> ArtifactTransactionResult | None:
        if not self._receipt_path.exists():
            return None
        if self._receipt_path.is_symlink():
            raise ArtifactTransactionIntegrityError(
                "transaction receipt cannot be a symbolic link"
            )
        if not self._receipt_path.is_file():
            raise ArtifactTransactionIntegrityError(
                "transaction receipt is not a regular file"
            )
        try:
            payload = self._receipt_path.read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            raise ArtifactTransactionIntegrityError(
                "transaction receipt cannot be read"
            ) from exc
        result = ArtifactTransactionResult.from_json(
            payload.strip()
        )
        result.verify_hash()
        request_hash = self.request.request_hash
        assert request_hash is not None
        if (
            result.transaction_id != self.request.transaction_id
            or result.request_hash != request_hash
            or result.status is not ArtifactTransactionStatus.COMMITTED
            or result.receipt_path != self._receipt_relative_path
        ):
            raise ArtifactTransactionIntegrityError(
                "existing transaction receipt does not match request"
            )
        return result

    def _verify_committed_destinations(
        self,
        result: ArtifactTransactionResult,
    ) -> None:
        payloads_by_id = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        for operation_result in result.operations:
            payload = payloads_by_id.get(
                operation_result.operation_id
            )
            if payload is None:
                raise ArtifactTransactionIntegrityError(
                    "receipt references an unknown payload operation"
                )
            destination = self._root.joinpath(
                *PurePosixPath(
                    operation_result.destination_path
                ).parts
            )
            _assert_inside_root(self._root, destination)
            if (
                _classify_lstat(destination)
                is not ArtifactWorkspaceEntryType.REGULAR_FILE
                or destination.stat().st_size != payload.size_bytes
                or _file_sha256(destination) != payload.content_sha256
            ):
                raise ArtifactTransactionIntegrityError(
                    "committed receipt final state no longer matches payload"
                )

    def _delete_retained_backups(
        self,
        applied: list[_AppliedOperation],
    ) -> None:
        for item in applied:
            if item.backup_path is None:
                continue
            try:
                if item.backup_path.exists():
                    if item.backup_path.is_symlink():
                        raise ArtifactTransactionIntegrityError(
                            "backup path became a symbolic link"
                        )
                    item.backup_path.unlink()
            except OSError as exc:
                raise ArtifactTransactionError(
                    "committed backup cleanup failed"
                ) from exc
