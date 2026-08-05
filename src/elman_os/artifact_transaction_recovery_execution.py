"""Execute deterministic recovery plans for interrupted artifact transactions.

The recovery executor accepts only a ``recoverable`` reconciliation result.
It acquires an exclusive recovery lock, revalidates every observed boundary,
performs the approved rollback/finalization/cleanup actions, and writes a
durable recovery receipt. Artifact content is never executed and no network
operation is performed.
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
    ArtifactApplicationOperation,
    ArtifactApplicationPlan,
)
from .artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerificationResult,
)
from .artifact_transaction_application import (
    ArtifactTransactionOperationResult,
    ArtifactTransactionOperationStatus,
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
    ArtifactTransactionResult,
    ArtifactTransactionStatus,
)
from .artifact_transaction_reconciliation import (
    ArtifactTransactionControlEntry,
    ArtifactTransactionControlKind,
    ArtifactTransactionControlState,
    ArtifactTransactionDestinationState,
    ArtifactTransactionReconciliationResult,
    ArtifactTransactionReconciliationStatus,
    ArtifactTransactionRecoveryAction,
    ArtifactTransactionRecoveryStrategy,
)
from .artifact_workspace_preflight import (
    ArtifactWorkspaceEntryType,
    ArtifactWorkspacePreflightResult,
)


ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactTransactionRecoveryError(RuntimeError):
    """A recovery contract or execution boundary is invalid."""


class ArtifactTransactionRecoveryIntegrityError(
    ArtifactTransactionRecoveryError
):
    """A recovery object, receipt, or observed file fails integrity."""


class ArtifactTransactionRecoveryLockError(
    ArtifactTransactionRecoveryError
):
    """The exclusive recovery lock cannot be acquired safely."""


class ArtifactTransactionRecoveryStatus(StrEnum):
    COMPLETED = "completed"
    NOOP = "noop"
    ROLLED_BACK = "rolled-back"
    FAILED = "failed"


class ArtifactTransactionRecoveryActionStatus(StrEnum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled-back"
    FAILED = "failed"


class ArtifactTransactionRecoveryActionKind(StrEnum):
    DELETE_CREATED_DESTINATION = "delete-created-destination"
    RESTORE_BACKUP = "restore-backup"
    FINALIZE_COMMITTED_RECEIPT = "finalize-committed-receipt"
    REMOVE_RESIDUAL_LOCK = "remove-residual-lock"
    REMOVE_TEMPORARY = "remove-temporary"
    REMOVE_VALID_BACKUP = "remove-valid-backup"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactTransactionRecoveryError(
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
        raise ArtifactTransactionRecoveryError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactTransactionRecoveryError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactTransactionRecoveryError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactTransactionRecoveryError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactTransactionRecoveryError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactTransactionRecoveryError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactTransactionRecoveryError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactTransactionRecoveryError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactTransactionRecoveryError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactTransactionRecoveryError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactTransactionRecoveryError(
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
        raise ArtifactTransactionRecoveryError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactTransactionRecoveryError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactTransactionRecoveryError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactTransactionRecoveryError(
                f"{name} contains a non-portable segment"
            )
    return path


def _normalize_workspace_root(
    value: str | os.PathLike[str],
) -> str:
    path = Path(value).expanduser()
    if not path.exists():
        raise ArtifactTransactionRecoveryError(
            "workspace_root must exist"
        )
    if path.is_symlink():
        raise ArtifactTransactionRecoveryError(
            "workspace_root cannot be a symbolic link"
        )
    if not path.is_dir():
        raise ArtifactTransactionRecoveryError(
            "workspace_root must be a directory"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactTransactionRecoveryError(
            "workspace_root cannot be resolved"
        ) from exc
    return resolved.as_posix()


def _assert_inside_root(root: Path, candidate: Path) -> None:
    try:
        common = os.path.commonpath(
            [str(root), str(candidate.resolve(strict=False))]
        )
    except (OSError, ValueError) as exc:
        raise ArtifactTransactionRecoveryError(
            "candidate path cannot be resolved safely"
        ) from exc
    if Path(common) != root:
        raise ArtifactTransactionRecoveryError(
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
        raise ArtifactTransactionRecoveryError(
            f"cannot read file for hashing: {path}"
        ) from exc
    return digest.hexdigest()


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _safe_mkdir_chain(root: Path, directory: Path) -> None:
    _assert_inside_root(root, directory)
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            entry_type = _classify_lstat(current)
            if entry_type is ArtifactWorkspaceEntryType.SYMLINK:
                raise ArtifactTransactionRecoveryError(
                    f"symbolic link detected in control path: {current}"
                )
            if entry_type is not ArtifactWorkspaceEntryType.DIRECTORY:
                raise ArtifactTransactionRecoveryError(
                    f"non-directory detected in control path: {current}"
                )
            continue
        try:
            current.mkdir()
        except FileExistsError:
            if (
                _classify_lstat(current)
                is not ArtifactWorkspaceEntryType.DIRECTORY
            ):
                raise ArtifactTransactionRecoveryError(
                    f"control path changed concurrently: {current}"
                )



@dataclass(frozen=True, slots=True)
class ArtifactTransactionRecoveryPolicy:
    policy_id: str
    control_root: str = ".elman-os"
    recovery_lock_name: str = "recovery.lock"
    recovery_receipt_directory: str = "recoveries"
    undo_directory: str = "recovery-undo"
    max_actions: int = 256
    fsync_files: bool = True
    remove_undo_on_success: bool = True
    remove_reconciliation_controls: bool = True
    version: int = ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION

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
        for field_name in (
            "recovery_lock_name",
            "recovery_receipt_directory",
            "undo_directory",
        ):
            value = _text(getattr(self, field_name), field_name)
            if (
                "/" in value
                or "\\" in value
                or _PORTABLE_SEGMENT.fullmatch(value) is None
            ):
                raise ArtifactTransactionRecoveryError(
                    f"{field_name} must be one portable segment"
                )
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self,
            "max_actions",
            _positive_int(self.max_actions, "max_actions"),
        )
        for field_name in (
            "fsync_files",
            "remove_undo_on_success",
            "remove_reconciliation_controls",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        if self.version != ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION:
            raise ArtifactTransactionRecoveryError(
                "unsupported recovery format version"
            )

    @property
    def recovery_lock_relative_path(self) -> str:
        return f"{self.control_root}/{self.recovery_lock_name}"

    @property
    def recovery_receipt_root(self) -> str:
        return (
            f"{self.control_root}/"
            f"{self.recovery_receipt_directory}"
        )

    @property
    def undo_root(self) -> str:
        return f"{self.control_root}/{self.undo_directory}"

    def recovery_receipt_relative_path(self, recovery_id: str) -> str:
        normalized = _identifier(recovery_id, "recovery_id")
        safe_name = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        return f"{self.recovery_receipt_root}/{safe_name}.json"

    def undo_relative_path(self, recovery_id: str) -> str:
        normalized = _identifier(recovery_id, "recovery_id")
        safe_name = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        return f"{self.undo_root}/{safe_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_recovery_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "control_root": self.control_root,
            "recovery_lock_name": self.recovery_lock_name,
            "recovery_receipt_directory": (
                self.recovery_receipt_directory
            ),
            "undo_directory": self.undo_directory,
            "max_actions": self.max_actions,
            "fsync_files": self.fsync_files,
            "remove_undo_on_success": self.remove_undo_on_success,
            "remove_reconciliation_controls": (
                self.remove_reconciliation_controls
            ),
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
    ) -> "ArtifactTransactionRecoveryPolicy":
        if data.get("record_type") != "artifact_transaction_recovery_policy":
            raise ArtifactTransactionRecoveryError(
                "record_type must be artifact_transaction_recovery_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            control_root=data["control_root"],
            recovery_lock_name=data["recovery_lock_name"],
            recovery_receipt_directory=data[
                "recovery_receipt_directory"
            ],
            undo_directory=data["undo_directory"],
            max_actions=data["max_actions"],
            fsync_files=data["fsync_files"],
            remove_undo_on_success=data[
                "remove_undo_on_success"
            ],
            remove_reconciliation_controls=data[
                "remove_reconciliation_controls"
            ],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionRecoveryPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionRecoveryError(
                "recovery policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionRecoveryRequest:
    recovery_id: str
    policy_id: str
    policy_hash: str
    reconciliation_id: str
    reconciliation_result_hash: str
    reconciliation_request_hash: str
    transaction_id: str
    transaction_request_hash: str
    transaction_policy_id: str
    transaction_policy_hash: str
    application_id: str
    application_plan_hash: str
    verification_id: str
    verification_result_hash: str
    preflight_id: str
    preflight_result_hash: str
    snapshot_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "recovery_id",
            "policy_id",
            "reconciliation_id",
            "transaction_id",
            "transaction_policy_id",
            "application_id",
            "verification_id",
            "preflight_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "policy_hash",
            "reconciliation_result_hash",
            "reconciliation_request_hash",
            "transaction_request_hash",
            "transaction_policy_hash",
            "application_plan_hash",
            "verification_result_hash",
            "preflight_result_hash",
            "snapshot_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
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
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionRecoveryError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(root).as_posix(),
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
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        if self.version != ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION:
            raise ArtifactTransactionRecoveryError(
                "unsupported recovery format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        reconciliation_result: ArtifactTransactionReconciliationResult,
        transaction_request: ArtifactTransactionRequest,
        transaction_policy: ArtifactTransactionPolicy,
        application_plan: ArtifactApplicationPlan,
        verification_result: ArtifactPayloadVerificationResult,
        preflight_result: ArtifactWorkspacePreflightResult,
        policy: ArtifactTransactionRecoveryPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        recovery_id: str | None = None,
    ) -> "ArtifactTransactionRecoveryRequest":
        reconciliation_result.verify_hash()
        transaction_request.verify_hash()
        transaction_request_hash = transaction_request.request_hash
        reconciliation_hash = reconciliation_result.result_hash
        plan_hash = application_plan.plan_hash
        verification_hash = verification_result.result_hash
        preflight_hash = preflight_result.result_hash
        assert transaction_request_hash is not None
        assert reconciliation_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None
        root = _normalize_workspace_root(
            reconciliation_result.workspace_root
        )
        requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(requested_at, "requested_at")
        identity_hash = _sha256_document(
            {
                "record_type": "artifact_transaction_recovery_identity",
                "policy_hash": policy.policy_hash,
                "reconciliation_result_hash": reconciliation_hash,
                "transaction_request_hash": transaction_request_hash,
                "workspace_root": root,
            }
        )
        effective_id = (
            recovery_id
            if recovery_id is not None
            else f"transaction-recovery:{identity_hash}"
        )
        return cls(
            recovery_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            reconciliation_id=reconciliation_result.reconciliation_id,
            reconciliation_result_hash=reconciliation_hash,
            reconciliation_request_hash=(
                reconciliation_result.request_hash
            ),
            transaction_id=transaction_request.transaction_id,
            transaction_request_hash=transaction_request_hash,
            transaction_policy_id=transaction_policy.policy_id,
            transaction_policy_hash=transaction_policy.policy_hash,
            application_id=application_plan.application_id,
            application_plan_hash=plan_hash,
            verification_id=verification_result.verification_id,
            verification_result_hash=verification_hash,
            preflight_id=preflight_result.preflight_id,
            preflight_result_hash=preflight_hash,
            snapshot_hash=preflight_result.snapshot_hash,
            plan_id=application_plan.plan_id,
            step_id=application_plan.step_id,
            agent_id=application_plan.agent_id,
            workspace_root=root,
            requested_by=requester,
            requested_at=timestamp,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_recovery_request",
            "version": self.version,
            "recovery_id": self.recovery_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "reconciliation_id": self.reconciliation_id,
            "reconciliation_result_hash": (
                self.reconciliation_result_hash
            ),
            "reconciliation_request_hash": (
                self.reconciliation_request_hash
            ),
            "transaction_id": self.transaction_id,
            "transaction_request_hash": self.transaction_request_hash,
            "transaction_policy_id": self.transaction_policy_id,
            "transaction_policy_hash": self.transaction_policy_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": (
                self.verification_result_hash
            ),
            "preflight_id": self.preflight_id,
            "preflight_result_hash": self.preflight_result_hash,
            "snapshot_hash": self.snapshot_hash,
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
            raise ArtifactTransactionRecoveryIntegrityError(
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
    ) -> "ArtifactTransactionRecoveryRequest":
        if data.get("record_type") != "artifact_transaction_recovery_request":
            raise ArtifactTransactionRecoveryError(
                "record_type must be artifact_transaction_recovery_request"
            )
        if "request_hash" not in data:
            raise ArtifactTransactionRecoveryIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            recovery_id=data["recovery_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            reconciliation_id=data["reconciliation_id"],
            reconciliation_result_hash=data[
                "reconciliation_result_hash"
            ],
            reconciliation_request_hash=data[
                "reconciliation_request_hash"
            ],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data[
                "transaction_request_hash"
            ],
            transaction_policy_id=data["transaction_policy_id"],
            transaction_policy_hash=data[
                "transaction_policy_hash"
            ],
            application_id=data["application_id"],
            application_plan_hash=data["application_plan_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data[
                "verification_result_hash"
            ],
            preflight_id=data["preflight_id"],
            preflight_result_hash=data["preflight_result_hash"],
            snapshot_hash=data["snapshot_hash"],
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
    ) -> "ArtifactTransactionRecoveryRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionRecoveryError(
                "recovery request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionRecoveryActionResult:
    index: int
    kind: ArtifactTransactionRecoveryActionKind
    target_path: str
    status: ArtifactTransactionRecoveryActionStatus
    before_sha256: str | None
    after_sha256: str | None
    bytes_changed: int
    reason: str
    action_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        try:
            kind_value = ArtifactTransactionRecoveryActionKind(
                self.kind
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery action kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind_value)
        object.__setattr__(
            self,
            "target_path",
            _portable_relative_path(
                self.target_path,
                "target_path",
            ),
        )
        try:
            status_value = ArtifactTransactionRecoveryActionStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery action status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        for field_name in ("before_sha256", "after_sha256"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _hash(value, field_name),
                )
        object.__setattr__(
            self,
            "bytes_changed",
            _non_negative_int(self.bytes_changed, "bytes_changed"),
        )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        computed = self.compute_hash()
        if self.action_hash is None:
            object.__setattr__(self, "action_hash", computed)
        else:
            supplied = _hash(self.action_hash, "action_hash")
            if supplied != computed:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "action hash does not match action content"
                )
            object.__setattr__(self, "action_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "target_path": self.target_path,
            "status": self.status.value,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "bytes_changed": self.bytes_changed,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.action_hash != self.compute_hash():
            raise ArtifactTransactionRecoveryIntegrityError(
                "action hash does not match action content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["action_hash"] = self.action_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionRecoveryActionResult":
        return cls(
            index=data["index"],
            kind=ArtifactTransactionRecoveryActionKind(data["kind"]),
            target_path=data["target_path"],
            status=ArtifactTransactionRecoveryActionStatus(
                data["status"]
            ),
            before_sha256=data.get("before_sha256"),
            after_sha256=data.get("after_sha256"),
            bytes_changed=data["bytes_changed"],
            reason=data["reason"],
            action_hash=data.get("action_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionRecoveryResult:
    recovery_id: str
    status: ArtifactTransactionRecoveryStatus
    strategy: ArtifactTransactionRecoveryStrategy
    request_hash: str
    policy_id: str
    policy_hash: str
    reconciliation_id: str
    reconciliation_result_hash: str
    transaction_id: str
    transaction_request_hash: str
    workspace_root: str
    recovery_lock_path: str
    recovery_receipt_path: str
    actions: tuple[ArtifactTransactionRecoveryActionResult, ...]
    applied_count: int
    skipped_count: int
    rolled_back_count: int
    failed_count: int
    started_at: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "recovery_id",
            "policy_id",
            "reconciliation_id",
            "transaction_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        try:
            status_value = ArtifactTransactionRecoveryStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        try:
            strategy_value = ArtifactTransactionRecoveryStrategy(
                self.strategy
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionRecoveryError(
                "recovery strategy is invalid"
            ) from exc
        object.__setattr__(self, "strategy", strategy_value)
        for field_name in (
            "request_hash",
            "policy_hash",
            "reconciliation_result_hash",
            "transaction_request_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionRecoveryError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(root).as_posix(),
        )
        object.__setattr__(
            self,
            "recovery_lock_path",
            _portable_relative_path(
                self.recovery_lock_path,
                "recovery_lock_path",
            ),
        )
        object.__setattr__(
            self,
            "recovery_receipt_path",
            _portable_relative_path(
                self.recovery_receipt_path,
                "recovery_receipt_path",
            ),
        )
        actions = tuple(self.actions)
        if not all(
            isinstance(item, ArtifactTransactionRecoveryActionResult)
            for item in actions
        ):
            raise ArtifactTransactionRecoveryError(
                "actions must contain recovery action results"
            )
        if tuple(item.index for item in actions) != tuple(
            range(len(actions))
        ):
            raise ArtifactTransactionRecoveryError(
                "recovery action indexes must be contiguous from zero"
            )
        for item in actions:
            item.verify_hash()
        object.__setattr__(self, "actions", actions)
        for field_name in (
            "applied_count",
            "skipped_count",
            "rolled_back_count",
            "failed_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(getattr(self, field_name), field_name),
            )
        actual = {
            ArtifactTransactionRecoveryActionStatus.APPLIED: 0,
            ArtifactTransactionRecoveryActionStatus.SKIPPED: 0,
            ArtifactTransactionRecoveryActionStatus.ROLLED_BACK: 0,
            ArtifactTransactionRecoveryActionStatus.FAILED: 0,
        }
        for item in actions:
            actual[item.status] += 1
        if (
            self.applied_count,
            self.skipped_count,
            self.rolled_back_count,
            self.failed_count,
        ) != (
            actual[ArtifactTransactionRecoveryActionStatus.APPLIED],
            actual[ArtifactTransactionRecoveryActionStatus.SKIPPED],
            actual[
                ArtifactTransactionRecoveryActionStatus.ROLLED_BACK
            ],
            actual[ArtifactTransactionRecoveryActionStatus.FAILED],
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery counts do not match action results"
            )
        if status_value in {
            ArtifactTransactionRecoveryStatus.COMPLETED,
            ArtifactTransactionRecoveryStatus.NOOP,
        } and (
            self.failed_count or self.rolled_back_count
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "successful recovery cannot contain failed actions"
            )
        if (
            status_value is ArtifactTransactionRecoveryStatus.ROLLED_BACK
            and not self.rolled_back_count
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "rolled-back recovery requires rolled-back actions"
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
        if self.version != ARTIFACT_TRANSACTION_RECOVERY_FORMAT_VERSION:
            raise ArtifactTransactionRecoveryError(
                "unsupported recovery format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_recovery_result",
            "version": self.version,
            "recovery_id": self.recovery_id,
            "status": self.status.value,
            "strategy": self.strategy.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "reconciliation_id": self.reconciliation_id,
            "reconciliation_result_hash": (
                self.reconciliation_result_hash
            ),
            "transaction_id": self.transaction_id,
            "transaction_request_hash": (
                self.transaction_request_hash
            ),
            "workspace_root": self.workspace_root,
            "recovery_lock_path": self.recovery_lock_path,
            "recovery_receipt_path": self.recovery_receipt_path,
            "actions": [item.to_dict() for item in self.actions],
            "applied_count": self.applied_count,
            "skipped_count": self.skipped_count,
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
            raise ArtifactTransactionRecoveryIntegrityError(
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
    ) -> "ArtifactTransactionRecoveryResult":
        if data.get("record_type") != "artifact_transaction_recovery_result":
            raise ArtifactTransactionRecoveryError(
                "record_type must be artifact_transaction_recovery_result"
            )
        if "result_hash" not in data:
            raise ArtifactTransactionRecoveryIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            recovery_id=data["recovery_id"],
            status=ArtifactTransactionRecoveryStatus(data["status"]),
            strategy=ArtifactTransactionRecoveryStrategy(
                data["strategy"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            reconciliation_id=data["reconciliation_id"],
            reconciliation_result_hash=data[
                "reconciliation_result_hash"
            ],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data[
                "transaction_request_hash"
            ],
            workspace_root=data["workspace_root"],
            recovery_lock_path=data["recovery_lock_path"],
            recovery_receipt_path=data["recovery_receipt_path"],
            actions=tuple(
                ArtifactTransactionRecoveryActionResult.from_dict(item)
                for item in data["actions"]
            ),
            applied_count=data["applied_count"],
            skipped_count=data["skipped_count"],
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
    ) -> "ArtifactTransactionRecoveryResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(slots=True)
class _UndoEntry:
    action_index: int
    target: Path
    undo: Path | None
    created_target: bool
    expected_after_sha256: str | None


@dataclass(frozen=True, slots=True)
class ArtifactTransactionRecoveryExecution:
    request: ArtifactTransactionRecoveryRequest
    reconciliation_result: ArtifactTransactionReconciliationResult
    transaction_request: ArtifactTransactionRequest
    transaction_policy: ArtifactTransactionPolicy
    application_plan: ArtifactApplicationPlan
    verification_result: ArtifactPayloadVerificationResult
    preflight_result: ArtifactWorkspacePreflightResult
    policy: ArtifactTransactionRecoveryPolicy

    def __post_init__(self) -> None:
        self.request.verify_hash()
        self.reconciliation_result.verify_hash()
        self.transaction_request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()
        self.preflight_result.verify_hash()
        if (
            self.reconciliation_result.status
            is not ArtifactTransactionReconciliationStatus.RECOVERABLE
        ):
            raise ArtifactTransactionRecoveryError(
                "reconciliation status must be recoverable"
            )
        if self.reconciliation_result.strategy not in {
            ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY,
            ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT,
            ArtifactTransactionRecoveryStrategy.ROLLBACK,
        }:
            raise ArtifactTransactionRecoveryError(
                "reconciliation strategy is not executable"
            )
        request_hash = self.transaction_request.request_hash
        reconciliation_hash = self.reconciliation_result.result_hash
        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        preflight_hash = self.preflight_result.result_hash
        assert request_hash is not None
        assert reconciliation_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None
        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "reconciliation_id": (
                self.reconciliation_result.reconciliation_id
            ),
            "reconciliation_result_hash": reconciliation_hash,
            "reconciliation_request_hash": (
                self.reconciliation_result.request_hash
            ),
            "transaction_id": self.transaction_request.transaction_id,
            "transaction_request_hash": request_hash,
            "transaction_policy_id": self.transaction_policy.policy_id,
            "transaction_policy_hash": self.transaction_policy.policy_hash,
            "application_id": self.application_plan.application_id,
            "application_plan_hash": plan_hash,
            "verification_id": self.verification_result.verification_id,
            "verification_result_hash": verification_hash,
            "preflight_id": self.preflight_result.preflight_id,
            "preflight_result_hash": preflight_hash,
            "snapshot_hash": self.preflight_result.snapshot_hash,
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
            "workspace_root": self.reconciliation_result.workspace_root,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactTransactionRecoveryError(
                    f"request {field_name} does not match recovery source"
                )
        if len(self.application_plan.operations) != len(
            self.reconciliation_result.records
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "reconciliation records do not cover application operations"
            )
        if len(self.application_plan.operations) != len(
            self.verification_result.payloads
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "payloads do not cover application operations"
            )
        action_count = sum(
            record.action is not ArtifactTransactionRecoveryAction.NONE
            for record in self.reconciliation_result.records
        ) + len(self.reconciliation_result.control_actions)
        if action_count > self.policy.max_actions:
            raise ArtifactTransactionRecoveryError(
                "recovery action count exceeds policy maximum"
            )

    @property
    def _root(self) -> Path:
        return Path(self.request.workspace_root)

    @property
    def _lock_relative_path(self) -> str:
        return self.policy.recovery_lock_relative_path

    @property
    def _lock_path(self) -> Path:
        return self._path(self._lock_relative_path)

    @property
    def _receipt_relative_path(self) -> str:
        return self.policy.recovery_receipt_relative_path(
            self.request.recovery_id
        )

    @property
    def _receipt_path(self) -> Path:
        return self._path(self._receipt_relative_path)

    @property
    def _undo_root(self) -> Path:
        return self._path(
            self.policy.undo_relative_path(self.request.recovery_id)
        )

    def execute(self) -> ArtifactTransactionRecoveryResult:
        existing = self._load_success_receipt()
        if existing is not None:
            self._verify_completed_state(existing)
            return existing

        root_text = _normalize_workspace_root(
            self.request.workspace_root
        )
        if root_text != self.request.workspace_root:
            raise ArtifactTransactionRecoveryError(
                "workspace root changed after recovery request creation"
            )
        self._acquire_lock()
        undo_entries: list[_UndoEntry] = []
        action_results: list[ArtifactTransactionRecoveryActionResult] = []
        started_at = self.request.requested_at
        try:
            self._revalidate_reconciliation_state()
            if self._undo_root.exists() or self._undo_root.is_symlink():
                raise ArtifactTransactionRecoveryError(
                    "recovery undo directory already exists"
                )
            _safe_mkdir_chain(self._root, self._undo_root)

            if (
                self.reconciliation_result.strategy
                is ArtifactTransactionRecoveryStrategy.ROLLBACK
            ):
                self._execute_operation_rollbacks(
                    action_results,
                    undo_entries,
                )
            elif (
                self.reconciliation_result.strategy
                is ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT
            ):
                self._execute_finalize_commit(
                    action_results,
                    undo_entries,
                )

            if self.policy.remove_reconciliation_controls:
                self._execute_control_cleanup(
                    action_results,
                    undo_entries,
                )

            status = (
                ArtifactTransactionRecoveryStatus.NOOP
                if not action_results
                else ArtifactTransactionRecoveryStatus.COMPLETED
            )
            result = self._build_result(
                status=status,
                actions=tuple(action_results),
                started_at=started_at,
                completed_at=self.request.requested_at,
                reason=(
                    "NOOP: recovery plan required no filesystem changes"
                    if status is ArtifactTransactionRecoveryStatus.NOOP
                    else "COMPLETED: recovery plan was applied and verified"
                ),
            )
            self._write_recovery_receipt(result)
            if self.policy.remove_undo_on_success:
                self._remove_undo_tree()
            return result
        except Exception as exc:
            rolled_back, rollback_failed = self._rollback_recovery(
                undo_entries,
                action_results,
                exc,
            )
            status = (
                ArtifactTransactionRecoveryStatus.FAILED
                if rollback_failed or not rolled_back
                else ArtifactTransactionRecoveryStatus.ROLLED_BACK
            )
            return self._build_result(
                status=status,
                actions=rolled_back,
                started_at=started_at,
                completed_at=self.request.requested_at,
                reason=(
                    f"{status.value.upper()}: recovery execution failed: {exc}"
                ),
            )
        finally:
            self._release_lock()

    def _path(self, relative_path: str) -> Path:
        normalized = _portable_relative_path(
            relative_path,
            "relative_path",
        )
        path = self._root.joinpath(
            *PurePosixPath(normalized).parts
        )
        _assert_inside_root(self._root, path)
        return path

    def _acquire_lock(self) -> None:
        _safe_mkdir_chain(self._root, self._lock_path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise ArtifactTransactionRecoveryLockError(
                "recovery lock already exists"
            ) from exc
        except OSError as exc:
            raise ArtifactTransactionRecoveryLockError(
                "recovery lock cannot be created"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    canonical_json(
                        {
                            "recovery_id": self.request.recovery_id,
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
                    raise ArtifactTransactionRecoveryLockError(
                        "recovery lock became a symbolic link"
                    )
                self._lock_path.unlink()
        except FileNotFoundError:
            return

    def _revalidate_reconciliation_state(self) -> None:
        payloads = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        snapshots = {
            entry.operation_id: entry
            for entry in self.preflight_result.snapshot
        }
        for operation, record in zip(
            self.application_plan.operations,
            self.reconciliation_result.records,
            strict=True,
        ):
            if (
                operation.sequence != record.sequence
                or operation.operation_id != record.operation_id
                or operation.destination_path != record.destination_path
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "reconciliation record does not align with operation"
                )
            payload = payloads[operation.operation_id]
            snapshot = snapshots[operation.operation_id]
            destination = self._path(operation.destination_path)
            entry_type = _classify_lstat(destination)
            if (
                record.destination_state
                is ArtifactTransactionDestinationState.BEFORE
            ):
                if operation.operation is ArtifactOperation.CREATE:
                    if entry_type is not ArtifactWorkspaceEntryType.ABSENT:
                        raise ArtifactTransactionRecoveryError(
                            "create destination changed after reconciliation"
                        )
                else:
                    if (
                        entry_type
                        is not ArtifactWorkspaceEntryType.REGULAR_FILE
                        or destination.stat().st_size
                        != snapshot.existing_size_bytes
                        or _file_sha256(destination)
                        != snapshot.existing_sha256
                    ):
                        raise ArtifactTransactionRecoveryError(
                            "update destination changed after reconciliation"
                        )
            elif (
                record.destination_state
                is ArtifactTransactionDestinationState.AFTER
            ):
                if (
                    entry_type
                    is not ArtifactWorkspaceEntryType.REGULAR_FILE
                    or destination.stat().st_size != payload.size_bytes
                    or _file_sha256(destination)
                    != payload.content_sha256
                ):
                    raise ArtifactTransactionRecoveryError(
                        "destination no longer matches verified payload"
                    )
            else:
                raise ArtifactTransactionRecoveryError(
                    "conflicted destination cannot be recovered"
                )
        for entry in self.reconciliation_result.control_entries:
            self._revalidate_control_entry(entry)

    def _revalidate_control_entry(
        self,
        entry: ArtifactTransactionControlEntry,
    ) -> None:
        path = self._path(entry.relative_path)
        entry_type = _classify_lstat(path)
        if entry_type is not entry.entry_type:
            raise ArtifactTransactionRecoveryError(
                "control entry type changed after reconciliation"
            )
        if entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
            if (
                path.stat().st_size != entry.size_bytes
                or _file_sha256(path) != entry.sha256
            ):
                raise ArtifactTransactionRecoveryError(
                    "control entry content changed after reconciliation"
                )

    def _execute_operation_rollbacks(
        self,
        results: list[ArtifactTransactionRecoveryActionResult],
        undo_entries: list[_UndoEntry],
    ) -> None:
        records = {
            record.operation_id: record
            for record in self.reconciliation_result.records
        }
        payloads = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        snapshots = {
            entry.operation_id: entry
            for entry in self.preflight_result.snapshot
        }
        for operation in reversed(self.application_plan.operations):
            record = records[operation.operation_id]
            if record.action is ArtifactTransactionRecoveryAction.NONE:
                continue
            payload = payloads[operation.operation_id]
            snapshot = snapshots[operation.operation_id]
            if (
                record.action
                is ArtifactTransactionRecoveryAction.DELETE_CREATED_DESTINATION
            ):
                result = self._delete_created_destination(
                    operation,
                    payload,
                    len(results),
                    undo_entries,
                )
            elif (
                record.action
                is ArtifactTransactionRecoveryAction.RESTORE_BACKUP
            ):
                result = self._restore_backup(
                    operation,
                    payload,
                    snapshot.existing_size_bytes,
                    snapshot.existing_sha256,
                    len(results),
                    undo_entries,
                )
            else:
                raise ArtifactTransactionRecoveryError(
                    "rollback strategy contains unsupported action"
                )
            results.append(result)

    def _delete_created_destination(
        self,
        operation: ArtifactApplicationOperation,
        payload: ArtifactPayload,
        index: int,
        undo_entries: list[_UndoEntry],
    ) -> ArtifactTransactionRecoveryActionResult:
        destination = self._path(operation.destination_path)
        self._assert_regular_file(
            destination,
            payload.size_bytes,
            payload.content_sha256,
            "created destination",
        )
        undo = self._save_undo_file(
            destination,
            index,
            payload.size_bytes,
            payload.content_sha256,
        )
        destination.unlink()
        if destination.exists() or destination.is_symlink():
            raise ArtifactTransactionRecoveryIntegrityError(
                "created destination was not removed"
            )
        undo_entries.append(
            _UndoEntry(
                action_index=index,
                target=destination,
                undo=undo,
                created_target=False,
                expected_after_sha256=None,
            )
        )
        return ArtifactTransactionRecoveryActionResult(
            index=index,
            kind=(
                ArtifactTransactionRecoveryActionKind.DELETE_CREATED_DESTINATION
            ),
            target_path=operation.destination_path,
            status=ArtifactTransactionRecoveryActionStatus.APPLIED,
            before_sha256=payload.content_sha256,
            after_sha256=None,
            bytes_changed=payload.size_bytes,
            reason=(
                "APPLIED: partially created destination was removed after "
                "SHA-256 revalidation"
            ),
        )

    def _restore_backup(
        self,
        operation: ArtifactApplicationOperation,
        payload: ArtifactPayload,
        before_size: int | None,
        before_sha256: str | None,
        index: int,
        undo_entries: list[_UndoEntry],
    ) -> ArtifactTransactionRecoveryActionResult:
        if (
            operation.backup_path is None
            or before_size is None
            or before_sha256 is None
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "update rollback boundary is incomplete"
            )
        destination = self._path(operation.destination_path)
        backup = self._path(operation.backup_path)
        self._assert_regular_file(
            destination,
            payload.size_bytes,
            payload.content_sha256,
            "updated destination",
        )
        self._assert_regular_file(
            backup,
            before_size,
            before_sha256,
            "verified backup",
        )
        undo = self._save_undo_file(
            destination,
            index,
            payload.size_bytes,
            payload.content_sha256,
        )
        self._atomic_replace_from_source(
            backup,
            destination,
            before_size,
            before_sha256,
        )
        self._assert_regular_file(
            destination,
            before_size,
            before_sha256,
            "restored destination",
        )
        undo_entries.append(
            _UndoEntry(
                action_index=index,
                target=destination,
                undo=undo,
                created_target=False,
                expected_after_sha256=before_sha256,
            )
        )
        return ArtifactTransactionRecoveryActionResult(
            index=index,
            kind=ArtifactTransactionRecoveryActionKind.RESTORE_BACKUP,
            target_path=operation.destination_path,
            status=ArtifactTransactionRecoveryActionStatus.APPLIED,
            before_sha256=payload.content_sha256,
            after_sha256=before_sha256,
            bytes_changed=before_size,
            reason=(
                "APPLIED: verified backup restored the preflight snapshot"
            ),
        )

    def _execute_finalize_commit(
        self,
        results: list[ArtifactTransactionRecoveryActionResult],
        undo_entries: list[_UndoEntry],
    ) -> None:
        receipt_relative = self.transaction_policy.receipt_relative_path(
            self.transaction_request.transaction_id
        )
        receipt_path = self._path(receipt_relative)
        if receipt_path.exists() or receipt_path.is_symlink():
            raise ArtifactTransactionRecoveryError(
                "transaction receipt appeared after reconciliation"
            )
        committed = self._build_committed_transaction_result()
        self._write_json_no_overwrite(receipt_path, committed.to_json())
        receipt_sha = _file_sha256(receipt_path)
        undo_entries.append(
            _UndoEntry(
                action_index=len(results),
                target=receipt_path,
                undo=None,
                created_target=True,
                expected_after_sha256=receipt_sha,
            )
        )
        results.append(
            ArtifactTransactionRecoveryActionResult(
                index=len(results),
                kind=(
                    ArtifactTransactionRecoveryActionKind.FINALIZE_COMMITTED_RECEIPT
                ),
                target_path=receipt_relative,
                status=ArtifactTransactionRecoveryActionStatus.APPLIED,
                before_sha256=None,
                after_sha256=receipt_sha,
                bytes_changed=receipt_path.stat().st_size,
                reason=(
                    "APPLIED: durable committed transaction receipt was "
                    "created after final-state revalidation"
                ),
            )
        )

    def _build_committed_transaction_result(
        self,
    ) -> ArtifactTransactionResult:
        payloads = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        snapshots = {
            entry.operation_id: entry
            for entry in self.preflight_result.snapshot
        }
        operation_results = tuple(
            ArtifactTransactionOperationResult(
                sequence=operation.sequence,
                operation_id=operation.operation_id,
                destination_path=operation.destination_path,
                operation=operation.operation,
                status=ArtifactTransactionOperationStatus.COMMITTED,
                payload_sha256=payloads[
                    operation.operation_id
                ].content_sha256,
                before_sha256=snapshots[
                    operation.operation_id
                ].existing_sha256,
                after_sha256=payloads[
                    operation.operation_id
                ].content_sha256,
                backup_path=operation.backup_path,
                bytes_written=payloads[
                    operation.operation_id
                ].size_bytes,
                reason=(
                    "COMMITTED: final artifact state was reconciled and "
                    "verified during recovery"
                ),
            )
            for operation in self.application_plan.operations
        )
        request_hash = self.transaction_request.request_hash
        assert request_hash is not None
        return ArtifactTransactionResult(
            transaction_id=self.transaction_request.transaction_id,
            status=ArtifactTransactionStatus.COMMITTED,
            request_hash=request_hash,
            policy_id=self.transaction_policy.policy_id,
            policy_hash=self.transaction_policy.policy_hash,
            preflight_id=self.preflight_result.preflight_id,
            preflight_result_hash=(
                self.transaction_request.preflight_result_hash
            ),
            snapshot_hash=self.preflight_result.snapshot_hash,
            verification_id=self.verification_result.verification_id,
            verification_result_hash=(
                self.transaction_request.verification_result_hash
            ),
            payload_manifest_hash=(
                self.verification_result.payload_manifest_hash
            ),
            application_id=self.application_plan.application_id,
            application_plan_hash=(
                self.transaction_request.application_plan_hash
            ),
            plan_id=self.application_plan.plan_id,
            step_id=self.application_plan.step_id,
            agent_id=self.application_plan.agent_id,
            workspace_root=self.request.workspace_root,
            lock_path=self.transaction_policy.lock_relative_path,
            receipt_path=(
                self.transaction_policy.receipt_relative_path(
                    self.transaction_request.transaction_id
                )
            ),
            operations=operation_results,
            committed_count=len(operation_results),
            rolled_back_count=0,
            failed_count=0,
            started_at=self.transaction_request.requested_at,
            completed_at=self.request.requested_at,
            reason=(
                "COMMITTED: recovery finalized an already complete artifact "
                "transaction after full state verification"
            ),
        )

    def _execute_control_cleanup(
        self,
        results: list[ArtifactTransactionRecoveryActionResult],
        undo_entries: list[_UndoEntry],
    ) -> None:
        entries = {
            entry.relative_path: entry
            for entry in self.reconciliation_result.control_entries
        }
        for raw_action in self.reconciliation_result.control_actions:
            prefix, separator, relative_path = raw_action.partition(":")
            if not separator:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "control action has an invalid format"
                )
            if prefix == "WRITE_COMMITTED_RECEIPT":
                continue
            entry = entries.get(relative_path)
            if entry is None:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "control action references an unknown entry"
                )
            if prefix == "REMOVE_RESIDUAL_LOCK":
                expected_kind = ArtifactTransactionControlKind.LOCK
                kind = (
                    ArtifactTransactionRecoveryActionKind.REMOVE_RESIDUAL_LOCK
                )
            elif prefix == "REMOVE_TEMPORARY":
                expected_kind = ArtifactTransactionControlKind.TEMPORARY
                kind = ArtifactTransactionRecoveryActionKind.REMOVE_TEMPORARY
            elif prefix == "REMOVE_VALID_BACKUP_AFTER_RECOVERY":
                expected_kind = ArtifactTransactionControlKind.BACKUP
                kind = (
                    ArtifactTransactionRecoveryActionKind.REMOVE_VALID_BACKUP
                )
            else:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "control action is not supported"
                )
            if entry.kind is not expected_kind:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "control action kind does not match control entry"
                )
            if entry.state not in {
                ArtifactTransactionControlState.RESIDUAL,
                ArtifactTransactionControlState.MATCHING,
            }:
                raise ArtifactTransactionRecoveryIntegrityError(
                    "control action does not reference removable state"
                )
            results.append(
                self._remove_control_entry(
                    entry,
                    kind,
                    len(results),
                    undo_entries,
                )
            )

    def _remove_control_entry(
        self,
        entry: ArtifactTransactionControlEntry,
        kind: ArtifactTransactionRecoveryActionKind,
        index: int,
        undo_entries: list[_UndoEntry],
    ) -> ArtifactTransactionRecoveryActionResult:
        path = self._path(entry.relative_path)
        if (
            entry.entry_type
            is not ArtifactWorkspaceEntryType.REGULAR_FILE
            or entry.size_bytes is None
            or entry.sha256 is None
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "removable control entry must be a regular file"
            )
        self._assert_regular_file(
            path,
            entry.size_bytes,
            entry.sha256,
            "control entry",
        )
        undo = self._save_undo_file(
            path,
            index,
            entry.size_bytes,
            entry.sha256,
        )
        path.unlink()
        if path.exists() or path.is_symlink():
            raise ArtifactTransactionRecoveryIntegrityError(
                "control entry was not removed"
            )
        undo_entries.append(
            _UndoEntry(
                action_index=index,
                target=path,
                undo=undo,
                created_target=False,
                expected_after_sha256=None,
            )
        )
        return ArtifactTransactionRecoveryActionResult(
            index=index,
            kind=kind,
            target_path=entry.relative_path,
            status=ArtifactTransactionRecoveryActionStatus.APPLIED,
            before_sha256=entry.sha256,
            after_sha256=None,
            bytes_changed=entry.size_bytes,
            reason="APPLIED: reconciled control file was removed safely",
        )

    def _save_undo_file(
        self,
        source: Path,
        action_index: int,
        expected_size: int,
        expected_sha256: str,
    ) -> Path:
        undo = self._undo_root / f"{action_index:06d}.undo"
        _assert_inside_root(self._root, undo)
        _safe_mkdir_chain(self._root, undo.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".elman-recovery-undo-",
            suffix=".tmp",
            dir=undo.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                with source.open("rb") as original:
                    shutil.copyfileobj(
                        original,
                        target,
                        length=1024 * 1024,
                    )
                if self.policy.fsync_files:
                    _fsync_file(target)
            if (
                temporary.stat().st_size != expected_size
                or _file_sha256(temporary) != expected_sha256
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "undo copy verification failed"
                )
            os.link(temporary, undo)
            return undo
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _atomic_replace_from_source(
        self,
        source: Path,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".elman-recovery-restore-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                with source.open("rb") as original:
                    shutil.copyfileobj(
                        original,
                        target,
                        length=1024 * 1024,
                    )
                if self.policy.fsync_files:
                    _fsync_file(target)
            if (
                temporary.stat().st_size != expected_size
                or _file_sha256(temporary) != expected_sha256
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "restore temporary does not match expected content"
                )
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _assert_regular_file(
        self,
        path: Path,
        expected_size: int,
        expected_sha256: str,
        label: str,
    ) -> None:
        if (
            _classify_lstat(path)
            is not ArtifactWorkspaceEntryType.REGULAR_FILE
        ):
            raise ArtifactTransactionRecoveryError(
                f"{label} is not a regular file"
            )
        if (
            path.stat().st_size != expected_size
            or _file_sha256(path) != expected_sha256
        ):
            raise ArtifactTransactionRecoveryError(
                f"{label} changed after reconciliation"
            )

    def _rollback_recovery(
        self,
        undo_entries: list[_UndoEntry],
        applied_results: list[ArtifactTransactionRecoveryActionResult],
        cause: Exception,
    ) -> tuple[
        tuple[ArtifactTransactionRecoveryActionResult, ...],
        bool,
    ]:
        result_by_index = {
            result.index: result
            for result in applied_results
        }
        rollback_failed = False
        for entry in reversed(undo_entries):
            original = result_by_index.get(entry.action_index)
            if original is None:
                continue
            try:
                if entry.created_target:
                    if (
                        _classify_lstat(entry.target)
                        is not ArtifactWorkspaceEntryType.REGULAR_FILE
                    ):
                        raise ArtifactTransactionRecoveryIntegrityError(
                            "created recovery target changed before rollback"
                        )
                    if (
                        entry.expected_after_sha256 is not None
                        and _file_sha256(entry.target)
                        != entry.expected_after_sha256
                    ):
                        raise ArtifactTransactionRecoveryIntegrityError(
                            "created recovery target hash changed"
                        )
                    entry.target.unlink()
                else:
                    if entry.undo is None:
                        raise ArtifactTransactionRecoveryIntegrityError(
                            "undo file is unavailable"
                        )
                    if entry.target.exists() or entry.target.is_symlink():
                        if entry.expected_after_sha256 is None:
                            raise ArtifactTransactionRecoveryIntegrityError(
                                "recovery target appeared before rollback"
                            )
                        if (
                            _classify_lstat(entry.target)
                            is not ArtifactWorkspaceEntryType.REGULAR_FILE
                            or _file_sha256(entry.target)
                            != entry.expected_after_sha256
                        ):
                            raise ArtifactTransactionRecoveryIntegrityError(
                                "recovery target changed before rollback"
                            )
                    _safe_mkdir_chain(self._root, entry.target.parent)
                    os.replace(entry.undo, entry.target)
                result_by_index[entry.action_index] = (
                    ArtifactTransactionRecoveryActionResult(
                        index=original.index,
                        kind=original.kind,
                        target_path=original.target_path,
                        status=(
                            ArtifactTransactionRecoveryActionStatus.ROLLED_BACK
                        ),
                        before_sha256=original.after_sha256,
                        after_sha256=original.before_sha256,
                        bytes_changed=original.bytes_changed,
                        reason=(
                            "ROLLED-BACK: recovery action was reversed after "
                            f"execution failure: {cause}"
                        ),
                    )
                )
            except Exception as rollback_exc:
                rollback_failed = True
                result_by_index[entry.action_index] = (
                    ArtifactTransactionRecoveryActionResult(
                        index=original.index,
                        kind=original.kind,
                        target_path=original.target_path,
                        status=ArtifactTransactionRecoveryActionStatus.FAILED,
                        before_sha256=original.before_sha256,
                        after_sha256=(
                            _file_sha256(entry.target)
                            if _classify_lstat(entry.target)
                            is ArtifactWorkspaceEntryType.REGULAR_FILE
                            else None
                        ),
                        bytes_changed=original.bytes_changed,
                        reason=(
                            "FAILED: recovery rollback could not complete: "
                            f"{rollback_exc}"
                        ),
                    )
                )
        ordered = tuple(
            result_by_index[index]
            for index in sorted(result_by_index)
        )
        try:
            self._remove_undo_tree()
        except OSError:
            rollback_failed = True
        return ordered, rollback_failed

    def _remove_undo_tree(self) -> None:
        if not self._undo_root.exists():
            return
        if self._undo_root.is_symlink():
            raise ArtifactTransactionRecoveryIntegrityError(
                "undo root became a symbolic link"
            )
        shutil.rmtree(self._undo_root)

    def _write_json_no_overwrite(
        self,
        destination: Path,
        payload: str,
    ) -> None:
        _safe_mkdir_chain(self._root, destination.parent)
        if destination.exists() or destination.is_symlink():
            raise ArtifactTransactionRecoveryIntegrityError(
                "durable receipt already exists"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".elman-recovery-receipt-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(payload)
                handle.write("\n")
                if self.policy.fsync_files:
                    _fsync_file(handle)
            os.link(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_recovery_receipt(
        self,
        result: ArtifactTransactionRecoveryResult,
    ) -> None:
        if result.status not in {
            ArtifactTransactionRecoveryStatus.COMPLETED,
            ArtifactTransactionRecoveryStatus.NOOP,
        }:
            raise ArtifactTransactionRecoveryIntegrityError(
                "only successful recovery results may be durable receipts"
            )
        self._write_json_no_overwrite(
            self._receipt_path,
            result.to_json(),
        )

    def _load_success_receipt(
        self,
    ) -> ArtifactTransactionRecoveryResult | None:
        if not self._receipt_path.exists():
            return None
        if self._receipt_path.is_symlink() or not self._receipt_path.is_file():
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery receipt is not a regular file"
            )
        try:
            payload = self._receipt_path.read_text(
                encoding="utf-8"
            ).strip()
        except (OSError, UnicodeError) as exc:
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery receipt cannot be read"
            ) from exc
        try:
            result = ArtifactTransactionRecoveryResult.from_json(payload)
            result.verify_hash()
        except ArtifactTransactionRecoveryError as exc:
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery receipt content or integrity is invalid"
            ) from exc
        request_hash = self.request.request_hash
        assert request_hash is not None
        if (
            result.recovery_id != self.request.recovery_id
            or result.request_hash != request_hash
            or result.recovery_receipt_path
            != self._receipt_relative_path
            or result.status
            not in {
                ArtifactTransactionRecoveryStatus.COMPLETED,
                ArtifactTransactionRecoveryStatus.NOOP,
            }
        ):
            raise ArtifactTransactionRecoveryIntegrityError(
                "recovery receipt does not match request"
            )
        return result

    def _verify_completed_state(
        self,
        result: ArtifactTransactionRecoveryResult,
    ) -> None:
        for action in result.actions:
            if action.kind in {
                ArtifactTransactionRecoveryActionKind.DELETE_CREATED_DESTINATION,
                ArtifactTransactionRecoveryActionKind.REMOVE_RESIDUAL_LOCK,
                ArtifactTransactionRecoveryActionKind.REMOVE_TEMPORARY,
                ArtifactTransactionRecoveryActionKind.REMOVE_VALID_BACKUP,
            } and (
                self._path(action.target_path).exists()
                or self._path(action.target_path).is_symlink()
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "completed recovery removal target has reappeared"
                )
        strategy = result.strategy
        if strategy is ArtifactTransactionRecoveryStrategy.ROLLBACK:
            for operation, snapshot in zip(
                self.application_plan.operations,
                self.preflight_result.snapshot,
                strict=True,
            ):
                destination = self._path(operation.destination_path)
                if operation.operation is ArtifactOperation.CREATE:
                    if (
                        _classify_lstat(destination)
                        is not ArtifactWorkspaceEntryType.ABSENT
                    ):
                        raise ArtifactTransactionRecoveryIntegrityError(
                            "completed rollback create state no longer matches"
                        )
                else:
                    if (
                        _classify_lstat(destination)
                        is not ArtifactWorkspaceEntryType.REGULAR_FILE
                        or destination.stat().st_size
                        != snapshot.existing_size_bytes
                        or _file_sha256(destination)
                        != snapshot.existing_sha256
                    ):
                        raise ArtifactTransactionRecoveryIntegrityError(
                            "completed rollback update state no longer matches"
                        )
        elif strategy is ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY:
            pass
        elif (
            strategy
            is ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT
        ):
            receipt = self._path(
                self.transaction_policy.receipt_relative_path(
                    self.transaction_request.transaction_id
                )
            )
            if (
                _classify_lstat(receipt)
                is not ArtifactWorkspaceEntryType.REGULAR_FILE
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "finalized transaction receipt is missing"
                )
            parsed = ArtifactTransactionResult.from_json(
                receipt.read_text(encoding="utf-8").strip()
            )
            if (
                parsed.status is not ArtifactTransactionStatus.COMMITTED
                or parsed.transaction_id
                != self.transaction_request.transaction_id
            ):
                raise ArtifactTransactionRecoveryIntegrityError(
                    "finalized transaction receipt is invalid"
                )

    def _build_result(
        self,
        *,
        status: ArtifactTransactionRecoveryStatus,
        actions: tuple[ArtifactTransactionRecoveryActionResult, ...],
        started_at: str,
        completed_at: str,
        reason: str,
    ) -> ArtifactTransactionRecoveryResult:
        request_hash = self.request.request_hash
        reconciliation_hash = self.reconciliation_result.result_hash
        transaction_request_hash = self.transaction_request.request_hash
        assert request_hash is not None
        assert reconciliation_hash is not None
        assert transaction_request_hash is not None
        return ArtifactTransactionRecoveryResult(
            recovery_id=self.request.recovery_id,
            status=status,
            strategy=self.reconciliation_result.strategy,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            reconciliation_id=(
                self.reconciliation_result.reconciliation_id
            ),
            reconciliation_result_hash=reconciliation_hash,
            transaction_id=self.transaction_request.transaction_id,
            transaction_request_hash=transaction_request_hash,
            workspace_root=self.request.workspace_root,
            recovery_lock_path=self._lock_relative_path,
            recovery_receipt_path=self._receipt_relative_path,
            actions=actions,
            applied_count=sum(
                item.status
                is ArtifactTransactionRecoveryActionStatus.APPLIED
                for item in actions
            ),
            skipped_count=sum(
                item.status
                is ArtifactTransactionRecoveryActionStatus.SKIPPED
                for item in actions
            ),
            rolled_back_count=sum(
                item.status
                is ArtifactTransactionRecoveryActionStatus.ROLLED_BACK
                for item in actions
            ),
            failed_count=sum(
                item.status
                is ArtifactTransactionRecoveryActionStatus.FAILED
                for item in actions
            ),
            started_at=started_at,
            completed_at=completed_at,
            reason=reason,
        )
