"""Read-only reconciliation of interrupted artifact transactions.

The reconciler inspects one transaction boundary without mutating the
workspace. It classifies the observed state as clean, committed, recoverable,
or conflicted and emits a deterministic recovery plan for a later executor.
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
from .artifact_transaction_application import (
    ArtifactTransactionError,
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
    ArtifactTransactionResult,
    ArtifactTransactionStatus,
)
from .artifact_workspace_preflight import (
    ArtifactWorkspaceEntryType,
    ArtifactWorkspacePreflightResult,
    ArtifactWorkspacePreflightStatus,
)


ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactTransactionReconciliationError(ValueError):
    """A reconciliation policy, request, record, or result is invalid."""


class ArtifactTransactionReconciliationIntegrityError(
    ArtifactTransactionReconciliationError
):
    """A reconciliation object or observed control file fails integrity."""


class ArtifactTransactionReconciliationStatus(StrEnum):
    CLEAN = "clean"
    COMMITTED = "committed"
    RECOVERABLE = "recoverable"
    CONFLICTED = "conflicted"


class ArtifactTransactionRecoveryStrategy(StrEnum):
    NONE = "none"
    CLEANUP_ONLY = "cleanup-only"
    FINALIZE_COMMIT = "finalize-commit"
    ROLLBACK = "rollback"
    MANUAL_REVIEW = "manual-review"


class ArtifactTransactionControlKind(StrEnum):
    LOCK = "lock"
    RECEIPT = "receipt"
    BACKUP = "backup"
    TEMPORARY = "temporary"


class ArtifactTransactionControlState(StrEnum):
    ABSENT = "absent"
    MATCHING = "matching"
    RESIDUAL = "residual"
    INVALID = "invalid"


class ArtifactTransactionDestinationState(StrEnum):
    BEFORE = "before"
    AFTER = "after"
    CONFLICTED = "conflicted"


class ArtifactTransactionBackupState(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


class ArtifactTransactionRecoveryAction(StrEnum):
    NONE = "none"
    DELETE_CREATED_DESTINATION = "delete-created-destination"
    RESTORE_BACKUP = "restore-backup"
    FINALIZE_COMMIT = "finalize-commit"
    INVESTIGATE = "investigate"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactTransactionReconciliationError(
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
        raise ArtifactTransactionReconciliationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactTransactionReconciliationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactTransactionReconciliationError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactTransactionReconciliationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactTransactionReconciliationError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactTransactionReconciliationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactTransactionReconciliationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactTransactionReconciliationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactTransactionReconciliationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactTransactionReconciliationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactTransactionReconciliationError(
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
        raise ArtifactTransactionReconciliationError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactTransactionReconciliationError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactTransactionReconciliationError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactTransactionReconciliationError(
                f"{name} contains a non-portable segment"
            )
    return path


def _normalize_workspace_root(
    value: str | os.PathLike[str],
) -> str:
    path = Path(value).expanduser()
    if not path.exists():
        raise ArtifactTransactionReconciliationError(
            "workspace_root must exist"
        )
    if path.is_symlink():
        raise ArtifactTransactionReconciliationError(
            "workspace_root cannot be a symbolic link"
        )
    if not path.is_dir():
        raise ArtifactTransactionReconciliationError(
            "workspace_root must be a directory"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactTransactionReconciliationError(
            "workspace_root cannot be resolved"
        ) from exc
    return resolved.as_posix()


def _assert_inside_root(root: Path, candidate: Path) -> None:
    try:
        common = os.path.commonpath(
            [str(root), str(candidate.resolve(strict=False))]
        )
    except (OSError, ValueError) as exc:
        raise ArtifactTransactionReconciliationError(
            "candidate path cannot be resolved safely"
        ) from exc
    if Path(common) != root:
        raise ArtifactTransactionReconciliationError(
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
        raise ArtifactTransactionReconciliationError(
            f"cannot read file for hashing: {path}"
        ) from exc
    return digest.hexdigest()


def _read_limited_text(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactTransactionReconciliationError(
            f"cannot stat control file: {path}"
        ) from exc
    if size > limit:
        raise ArtifactTransactionReconciliationIntegrityError(
            f"control file exceeds inspection limit: {path}"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArtifactTransactionReconciliationIntegrityError(
            f"control file is not readable UTF-8: {path}"
        ) from exc


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
        raise ArtifactTransactionReconciliationError(
            f"cannot inspect directory entries: {parent}"
        ) from exc
    return tuple(
        sorted(
            entry.name
            for entry in entries
            if (
                entry.name.casefold() == target.name.casefold()
                and entry.name != target.name
            )
        )
    )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionReconciliationPolicy:
    policy_id: str
    max_control_file_bytes: int = 1_000_000
    max_temporary_entries: int = 128
    allow_finalize_without_receipt: bool = True
    plan_remove_residual_lock: bool = True
    plan_remove_temporary_files: bool = True
    plan_remove_valid_backups_after_rollback: bool = True
    version: int = ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "max_control_file_bytes",
            "max_temporary_entries",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        for field_name in (
            "allow_finalize_without_receipt",
            "plan_remove_residual_lock",
            "plan_remove_temporary_files",
            "plan_remove_valid_backups_after_rollback",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        if (
            self.version
            != ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION
        ):
            raise ArtifactTransactionReconciliationError(
                "unsupported reconciliation format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_reconciliation_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "max_control_file_bytes": self.max_control_file_bytes,
            "max_temporary_entries": self.max_temporary_entries,
            "allow_finalize_without_receipt": (
                self.allow_finalize_without_receipt
            ),
            "plan_remove_residual_lock": (
                self.plan_remove_residual_lock
            ),
            "plan_remove_temporary_files": (
                self.plan_remove_temporary_files
            ),
            "plan_remove_valid_backups_after_rollback": (
                self.plan_remove_valid_backups_after_rollback
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
    ) -> "ArtifactTransactionReconciliationPolicy":
        if (
            data.get("record_type")
            != "artifact_transaction_reconciliation_policy"
        ):
            raise ArtifactTransactionReconciliationError(
                "record_type must be artifact_transaction_reconciliation_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            max_control_file_bytes=data["max_control_file_bytes"],
            max_temporary_entries=data["max_temporary_entries"],
            allow_finalize_without_receipt=data[
                "allow_finalize_without_receipt"
            ],
            plan_remove_residual_lock=data[
                "plan_remove_residual_lock"
            ],
            plan_remove_temporary_files=data[
                "plan_remove_temporary_files"
            ],
            plan_remove_valid_backups_after_rollback=data[
                "plan_remove_valid_backups_after_rollback"
            ],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionReconciliationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionReconciliationError(
                "reconciliation policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionReconciliationError(
                "reconciliation policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionReconciliationRequest:
    reconciliation_id: str
    policy_id: str
    policy_hash: str
    transaction_id: str
    transaction_request_hash: str
    transaction_policy_id: str
    transaction_policy_hash: str
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
    version: int = ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "reconciliation_id",
            "policy_id",
            "transaction_id",
            "transaction_policy_id",
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
            "transaction_request_hash",
            "transaction_policy_hash",
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
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionReconciliationError(
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
        if (
            self.version
            != ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION
        ):
            raise ArtifactTransactionReconciliationError(
                "unsupported reconciliation format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        transaction_request: ArtifactTransactionRequest,
        application_plan: ArtifactApplicationPlan,
        verification_result: ArtifactPayloadVerificationResult,
        preflight_result: ArtifactWorkspacePreflightResult,
        transaction_policy: ArtifactTransactionPolicy,
        policy: ArtifactTransactionReconciliationPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        reconciliation_id: str | None = None,
    ) -> "ArtifactTransactionReconciliationRequest":
        if not isinstance(
            transaction_request,
            ArtifactTransactionRequest,
        ):
            raise ArtifactTransactionReconciliationError(
                "transaction_request must be an ArtifactTransactionRequest"
            )
        if not isinstance(application_plan, ArtifactApplicationPlan):
            raise ArtifactTransactionReconciliationError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactTransactionReconciliationError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(
            preflight_result,
            ArtifactWorkspacePreflightResult,
        ):
            raise ArtifactTransactionReconciliationError(
                "preflight_result must be an ArtifactWorkspacePreflightResult"
            )
        if not isinstance(transaction_policy, ArtifactTransactionPolicy):
            raise ArtifactTransactionReconciliationError(
                "transaction_policy must be an ArtifactTransactionPolicy"
            )
        if not isinstance(
            policy,
            ArtifactTransactionReconciliationPolicy,
        ):
            raise ArtifactTransactionReconciliationError(
                "policy must be an ArtifactTransactionReconciliationPolicy"
            )
        transaction_request.verify_hash()
        application_plan.verify_hash()
        verification_result.verify_hash()
        preflight_result.verify_hash()

        transaction_request_hash = transaction_request.request_hash
        plan_hash = application_plan.plan_hash
        verification_hash = verification_result.result_hash
        preflight_hash = preflight_result.result_hash
        assert transaction_request_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None

        normalized_root = _normalize_workspace_root(
            transaction_request.workspace_root
        )
        requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(requested_at, "requested_at")
        identity_hash = _sha256_document(
            {
                "record_type": "artifact_transaction_reconciliation_identity",
                "policy_hash": policy.policy_hash,
                "transaction_id": transaction_request.transaction_id,
                "transaction_request_hash": transaction_request_hash,
                "workspace_root": normalized_root,
            }
        )
        effective_id = (
            reconciliation_id
            if reconciliation_id is not None
            else f"transaction-reconciliation:{identity_hash}"
        )
        return cls(
            reconciliation_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            transaction_id=transaction_request.transaction_id,
            transaction_request_hash=transaction_request_hash,
            transaction_policy_id=transaction_policy.policy_id,
            transaction_policy_hash=transaction_policy.policy_hash,
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
            requested_by=requester,
            requested_at=timestamp,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_reconciliation_request",
            "version": self.version,
            "reconciliation_id": self.reconciliation_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "transaction_id": self.transaction_id,
            "transaction_request_hash": self.transaction_request_hash,
            "transaction_policy_id": self.transaction_policy_id,
            "transaction_policy_hash": self.transaction_policy_hash,
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
            raise ArtifactTransactionReconciliationIntegrityError(
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
    ) -> "ArtifactTransactionReconciliationRequest":
        if (
            data.get("record_type")
            != "artifact_transaction_reconciliation_request"
        ):
            raise ArtifactTransactionReconciliationError(
                "record_type must be artifact_transaction_reconciliation_request"
            )
        if "request_hash" not in data:
            raise ArtifactTransactionReconciliationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            reconciliation_id=data["reconciliation_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data[
                "transaction_request_hash"
            ],
            transaction_policy_id=data[
                "transaction_policy_id"
            ],
            transaction_policy_hash=data[
                "transaction_policy_hash"
            ],
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
    ) -> "ArtifactTransactionReconciliationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionReconciliationError(
                "reconciliation request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionReconciliationError(
                "reconciliation request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionControlEntry:
    index: int
    relative_path: str
    kind: ArtifactTransactionControlKind
    entry_type: ArtifactWorkspaceEntryType
    state: ArtifactTransactionControlState
    size_bytes: int | None
    sha256: str | None
    reason: str
    entry_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        object.__setattr__(
            self,
            "relative_path",
            _portable_relative_path(
                self.relative_path,
                "relative_path",
            ),
        )
        try:
            kind_value = ArtifactTransactionControlKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "control kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind_value)
        try:
            type_value = ArtifactWorkspaceEntryType(self.entry_type)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "control entry type is invalid"
            ) from exc
        object.__setattr__(self, "entry_type", type_value)
        try:
            state_value = ArtifactTransactionControlState(self.state)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "control state is invalid"
            ) from exc
        object.__setattr__(self, "state", state_value)
        if self.size_bytes is not None:
            object.__setattr__(
                self,
                "size_bytes",
                _non_negative_int(self.size_bytes, "size_bytes"),
            )
        if self.sha256 is not None:
            object.__setattr__(
                self,
                "sha256",
                _hash(self.sha256, "sha256"),
            )
        if type_value is ArtifactWorkspaceEntryType.REGULAR_FILE:
            if self.size_bytes is None or self.sha256 is None:
                raise ArtifactTransactionReconciliationError(
                    "regular control file requires size and sha256"
                )
        elif self.size_bytes is not None or self.sha256 is not None:
            raise ArtifactTransactionReconciliationError(
                "non-file control entry cannot contain size or sha256"
            )
        object.__setattr__(
            self,
            "reason",
            _text(self.reason, "reason"),
        )
        computed = self.compute_hash()
        if self.entry_hash is None:
            object.__setattr__(self, "entry_hash", computed)
        else:
            supplied = _hash(self.entry_hash, "entry_hash")
            if supplied != computed:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "control entry hash does not match content"
                )
            object.__setattr__(self, "entry_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "relative_path": self.relative_path,
            "kind": self.kind.value,
            "entry_type": self.entry_type.value,
            "state": self.state.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.entry_hash != self.compute_hash():
            raise ArtifactTransactionReconciliationIntegrityError(
                "control entry hash does not match content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["entry_hash"] = self.entry_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionControlEntry":
        return cls(
            index=data["index"],
            relative_path=data["relative_path"],
            kind=ArtifactTransactionControlKind(data["kind"]),
            entry_type=ArtifactWorkspaceEntryType(
                data["entry_type"]
            ),
            state=ArtifactTransactionControlState(data["state"]),
            size_bytes=data.get("size_bytes"),
            sha256=data.get("sha256"),
            reason=data["reason"],
            entry_hash=data.get("entry_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionReconciliationRecord:
    sequence: int
    operation_id: str
    destination_path: str
    operation: ArtifactOperation
    destination_state: ArtifactTransactionDestinationState
    current_size_bytes: int | None
    current_sha256: str | None
    backup_state: ArtifactTransactionBackupState
    backup_path: str | None
    action: ArtifactTransactionRecoveryAction
    reasons: tuple[str, ...]
    record_hash: str | None = None

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
            operation_value = ArtifactOperation(self.operation)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "operation is invalid"
            ) from exc
        object.__setattr__(self, "operation", operation_value)
        try:
            destination_state = ArtifactTransactionDestinationState(
                self.destination_state
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "destination_state is invalid"
            ) from exc
        object.__setattr__(
            self,
            "destination_state",
            destination_state,
        )
        if self.current_size_bytes is not None:
            object.__setattr__(
                self,
                "current_size_bytes",
                _non_negative_int(
                    self.current_size_bytes,
                    "current_size_bytes",
                ),
            )
        if self.current_sha256 is not None:
            object.__setattr__(
                self,
                "current_sha256",
                _hash(self.current_sha256, "current_sha256"),
            )
        if (
            self.current_size_bytes is None
        ) != (
            self.current_sha256 is None
        ):
            raise ArtifactTransactionReconciliationError(
                "current size and sha256 must be both present or absent"
            )
        try:
            backup_state = ArtifactTransactionBackupState(
                self.backup_state
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "backup_state is invalid"
            ) from exc
        object.__setattr__(self, "backup_state", backup_state)
        if self.backup_path is not None:
            object.__setattr__(
                self,
                "backup_path",
                _portable_relative_path(
                    self.backup_path,
                    "backup_path",
                ),
            )
        if operation_value is ArtifactOperation.CREATE:
            if (
                backup_state
                is not ArtifactTransactionBackupState.NOT_APPLICABLE
                or self.backup_path is not None
            ):
                raise ArtifactTransactionReconciliationError(
                    "create reconciliation cannot contain backup state"
                )
        elif self.backup_path is None:
            raise ArtifactTransactionReconciliationError(
                "update reconciliation requires backup_path"
            )
        try:
            action_value = ArtifactTransactionRecoveryAction(
                self.action
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "recovery action is invalid"
            ) from exc
        object.__setattr__(self, "action", action_value)
        reasons = tuple(
            dict.fromkeys(
                _text(item, "reason")
                for item in self.reasons
            )
        )
        if not reasons:
            raise ArtifactTransactionReconciliationError(
                "record must contain at least one reason"
            )
        object.__setattr__(self, "reasons", reasons)
        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "reconciliation record hash does not match content"
                )
            object.__setattr__(self, "record_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "operation": self.operation.value,
            "destination_state": self.destination_state.value,
            "current_size_bytes": self.current_size_bytes,
            "current_sha256": self.current_sha256,
            "backup_state": self.backup_state.value,
            "backup_path": self.backup_path,
            "action": self.action.value,
            "reasons": list(self.reasons),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise ArtifactTransactionReconciliationIntegrityError(
                "reconciliation record hash does not match content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionReconciliationRecord":
        return cls(
            sequence=data["sequence"],
            operation_id=data["operation_id"],
            destination_path=data["destination_path"],
            operation=ArtifactOperation(data["operation"]),
            destination_state=ArtifactTransactionDestinationState(
                data["destination_state"]
            ),
            current_size_bytes=data.get("current_size_bytes"),
            current_sha256=data.get("current_sha256"),
            backup_state=ArtifactTransactionBackupState(
                data["backup_state"]
            ),
            backup_path=data.get("backup_path"),
            action=ArtifactTransactionRecoveryAction(
                data["action"]
            ),
            reasons=tuple(data["reasons"]),
            record_hash=data.get("record_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionReconciliationResult:
    reconciliation_id: str
    status: ArtifactTransactionReconciliationStatus
    strategy: ArtifactTransactionRecoveryStrategy
    request_hash: str
    policy_id: str
    policy_hash: str
    transaction_id: str
    transaction_request_hash: str
    transaction_policy_id: str
    transaction_policy_hash: str
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
    control_entries: tuple[ArtifactTransactionControlEntry, ...]
    records: tuple[ArtifactTransactionReconciliationRecord, ...]
    control_actions: tuple[str, ...]
    top_level_reasons: tuple[str, ...]
    before_count: int
    after_count: int
    conflicted_count: int
    inspected_at: str
    result_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "reconciliation_id",
            "policy_id",
            "transaction_id",
            "transaction_policy_id",
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
            status_value = ArtifactTransactionReconciliationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "reconciliation status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        try:
            strategy_value = ArtifactTransactionRecoveryStrategy(
                self.strategy
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionReconciliationError(
                "recovery strategy is invalid"
            ) from exc
        object.__setattr__(self, "strategy", strategy_value)
        for field_name in (
            "request_hash",
            "policy_hash",
            "transaction_request_hash",
            "transaction_policy_hash",
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
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionReconciliationError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(root).as_posix(),
        )

        entries = tuple(self.control_entries)
        if not all(
            isinstance(item, ArtifactTransactionControlEntry)
            for item in entries
        ):
            raise ArtifactTransactionReconciliationError(
                "control_entries must contain control entries"
            )
        if tuple(item.index for item in entries) != tuple(
            range(len(entries))
        ):
            raise ArtifactTransactionReconciliationError(
                "control entry indexes must be contiguous from zero"
            )
        for item in entries:
            item.verify_hash()
        object.__setattr__(self, "control_entries", entries)

        records = tuple(self.records)
        if not all(
            isinstance(item, ArtifactTransactionReconciliationRecord)
            for item in records
        ):
            raise ArtifactTransactionReconciliationError(
                "records must contain reconciliation records"
            )
        if tuple(item.sequence for item in records) != tuple(
            range(1, len(records) + 1)
        ):
            raise ArtifactTransactionReconciliationError(
                "record sequences must be contiguous from one"
            )
        for item in records:
            item.verify_hash()
        object.__setattr__(self, "records", records)

        actions = tuple(
            dict.fromkeys(
                _text(item, "control_action")
                for item in self.control_actions
            )
        )
        object.__setattr__(self, "control_actions", actions)
        reasons = tuple(
            dict.fromkeys(
                _text(item, "top_level_reason")
                for item in self.top_level_reasons
            )
        )
        object.__setattr__(self, "top_level_reasons", reasons)

        for field_name in (
            "before_count",
            "after_count",
            "conflicted_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        actual_before = sum(
            item.destination_state
            is ArtifactTransactionDestinationState.BEFORE
            for item in records
        )
        actual_after = sum(
            item.destination_state
            is ArtifactTransactionDestinationState.AFTER
            for item in records
        )
        actual_conflicted = sum(
            item.destination_state
            is ArtifactTransactionDestinationState.CONFLICTED
            for item in records
        )
        if (
            self.before_count,
            self.after_count,
            self.conflicted_count,
        ) != (
            actual_before,
            actual_after,
            actual_conflicted,
        ):
            raise ArtifactTransactionReconciliationIntegrityError(
                "reconciliation counts do not match records"
            )
        if status_value is ArtifactTransactionReconciliationStatus.CLEAN:
            if strategy_value is not ArtifactTransactionRecoveryStrategy.NONE:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "clean result must use none strategy"
                )
            if actual_after or actual_conflicted or actions:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "clean result cannot contain changes or actions"
                )
        if status_value is ArtifactTransactionReconciliationStatus.CONFLICTED:
            if (
                strategy_value
                is not ArtifactTransactionRecoveryStrategy.MANUAL_REVIEW
            ):
                raise ArtifactTransactionReconciliationIntegrityError(
                    "conflicted result must use manual-review strategy"
                )
        if status_value is ArtifactTransactionReconciliationStatus.COMMITTED:
            if actual_before or actual_conflicted:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "committed result requires every destination after"
                )
        object.__setattr__(
            self,
            "inspected_at",
            _utc_timestamp(self.inspected_at, "inspected_at"),
        )
        if (
            self.version
            != ARTIFACT_TRANSACTION_RECONCILIATION_FORMAT_VERSION
        ):
            raise ArtifactTransactionReconciliationError(
                "unsupported reconciliation format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_reconciliation_result",
            "version": self.version,
            "reconciliation_id": self.reconciliation_id,
            "status": self.status.value,
            "strategy": self.strategy.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "transaction_id": self.transaction_id,
            "transaction_request_hash": (
                self.transaction_request_hash
            ),
            "transaction_policy_id": self.transaction_policy_id,
            "transaction_policy_hash": (
                self.transaction_policy_hash
            ),
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
            "control_entries": [
                item.to_dict()
                for item in self.control_entries
            ],
            "records": [
                item.to_dict()
                for item in self.records
            ],
            "control_actions": list(self.control_actions),
            "top_level_reasons": list(self.top_level_reasons),
            "before_count": self.before_count,
            "after_count": self.after_count,
            "conflicted_count": self.conflicted_count,
            "inspected_at": self.inspected_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactTransactionReconciliationIntegrityError(
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
    ) -> "ArtifactTransactionReconciliationResult":
        if (
            data.get("record_type")
            != "artifact_transaction_reconciliation_result"
        ):
            raise ArtifactTransactionReconciliationError(
                "record_type must be artifact_transaction_reconciliation_result"
            )
        if "result_hash" not in data:
            raise ArtifactTransactionReconciliationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            reconciliation_id=data["reconciliation_id"],
            status=ArtifactTransactionReconciliationStatus(
                data["status"]
            ),
            strategy=ArtifactTransactionRecoveryStrategy(
                data["strategy"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data[
                "transaction_request_hash"
            ],
            transaction_policy_id=data[
                "transaction_policy_id"
            ],
            transaction_policy_hash=data[
                "transaction_policy_hash"
            ],
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
            control_entries=tuple(
                ArtifactTransactionControlEntry.from_dict(item)
                for item in data["control_entries"]
            ),
            records=tuple(
                ArtifactTransactionReconciliationRecord.from_dict(
                    item
                )
                for item in data["records"]
            ),
            control_actions=tuple(data["control_actions"]),
            top_level_reasons=tuple(data["top_level_reasons"]),
            before_count=data["before_count"],
            after_count=data["after_count"],
            conflicted_count=data["conflicted_count"],
            inspected_at=data["inspected_at"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionReconciliationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionReconciliationError(
                "reconciliation result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionReconciliationError(
                "reconciliation result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionReconciliation:
    request: ArtifactTransactionReconciliationRequest
    transaction_request: ArtifactTransactionRequest
    application_plan: ArtifactApplicationPlan
    verification_result: ArtifactPayloadVerificationResult
    preflight_result: ArtifactWorkspacePreflightResult
    transaction_policy: ArtifactTransactionPolicy
    policy: ArtifactTransactionReconciliationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactTransactionReconciliationRequest,
        ):
            raise ArtifactTransactionReconciliationError(
                "request must be an ArtifactTransactionReconciliationRequest"
            )
        if not isinstance(
            self.transaction_request,
            ArtifactTransactionRequest,
        ):
            raise ArtifactTransactionReconciliationError(
                "transaction_request must be an ArtifactTransactionRequest"
            )
        if not isinstance(
            self.application_plan,
            ArtifactApplicationPlan,
        ):
            raise ArtifactTransactionReconciliationError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            self.verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactTransactionReconciliationError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(
            self.preflight_result,
            ArtifactWorkspacePreflightResult,
        ):
            raise ArtifactTransactionReconciliationError(
                "preflight_result must be an ArtifactWorkspacePreflightResult"
            )
        if not isinstance(
            self.transaction_policy,
            ArtifactTransactionPolicy,
        ):
            raise ArtifactTransactionReconciliationError(
                "transaction_policy must be an ArtifactTransactionPolicy"
            )
        if not isinstance(
            self.policy,
            ArtifactTransactionReconciliationPolicy,
        ):
            raise ArtifactTransactionReconciliationError(
                "policy must be an ArtifactTransactionReconciliationPolicy"
            )

        self.request.verify_hash()
        self.transaction_request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()
        self.preflight_result.verify_hash()

        transaction_request_hash = self.transaction_request.request_hash
        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        preflight_hash = self.preflight_result.result_hash
        assert transaction_request_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None

        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "transaction_id": self.transaction_request.transaction_id,
            "transaction_request_hash": transaction_request_hash,
            "transaction_policy_id": self.transaction_policy.policy_id,
            "transaction_policy_hash": self.transaction_policy.policy_hash,
            "preflight_id": self.preflight_result.preflight_id,
            "preflight_result_hash": preflight_hash,
            "snapshot_hash": self.preflight_result.snapshot_hash,
            "verification_id": self.verification_result.verification_id,
            "verification_result_hash": verification_hash,
            "payload_manifest_hash": (
                self.verification_result.payload_manifest_hash
            ),
            "application_id": self.application_plan.application_id,
            "application_plan_hash": plan_hash,
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
            "workspace_root": self.transaction_request.workspace_root,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactTransactionReconciliationError(
                    f"request {field_name} does not match source boundary"
                )

        transaction_expected = {
            "policy_id": self.transaction_policy.policy_id,
            "policy_hash": self.transaction_policy.policy_hash,
            "preflight_id": self.preflight_result.preflight_id,
            "preflight_result_hash": preflight_hash,
            "snapshot_hash": self.preflight_result.snapshot_hash,
            "verification_id": self.verification_result.verification_id,
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
        for field_name, expected_value in transaction_expected.items():
            if getattr(self.transaction_request, field_name) != expected_value:
                raise ArtifactTransactionReconciliationError(
                    f"transaction request {field_name} does not match source"
                )

        if (
            self.application_plan.decision
            is not ArtifactApplicationDecision.READY
        ):
            raise ArtifactTransactionReconciliationError(
                "application plan decision must be ready"
            )
        if (
            self.verification_result.status
            is not ArtifactPayloadVerificationStatus.VERIFIED
        ):
            raise ArtifactTransactionReconciliationError(
                "payload verification status must be verified"
            )
        if (
            self.preflight_result.status
            is not ArtifactWorkspacePreflightStatus.READY
        ):
            raise ArtifactTransactionReconciliationError(
                "workspace preflight status must be ready"
            )
        if not self.application_plan.operations:
            raise ArtifactTransactionReconciliationError(
                "application plan contains no operations"
            )
        if len(self.application_plan.operations) != len(
            self.verification_result.payloads
        ):
            raise ArtifactTransactionReconciliationError(
                "operation and payload counts do not match"
            )
        if len(self.application_plan.operations) != len(
            self.preflight_result.snapshot
        ):
            raise ArtifactTransactionReconciliationError(
                "operation and snapshot counts do not match"
            )

    @property
    def _root(self) -> Path:
        return Path(self.request.workspace_root)

    def reconcile(self) -> ArtifactTransactionReconciliationResult:
        root_text = _normalize_workspace_root(
            self.request.workspace_root
        )
        if root_text != self.request.workspace_root:
            raise ArtifactTransactionReconciliationError(
                "workspace root changed after request creation"
            )
        root = Path(root_text)

        control_entries: list[ArtifactTransactionControlEntry] = []
        lock_entry = self._inspect_lock(
            len(control_entries),
            root,
        )
        control_entries.append(lock_entry)
        receipt_entry, receipt = self._inspect_receipt(
            len(control_entries),
            root,
        )
        control_entries.append(receipt_entry)

        backup_entries: dict[str, ArtifactTransactionControlEntry] = {}
        for operation, snapshot in zip(
            self.application_plan.operations,
            self.preflight_result.snapshot,
            strict=True,
        ):
            if operation.operation is not ArtifactOperation.UPDATE:
                continue
            entry = self._inspect_backup(
                len(control_entries),
                root,
                operation,
                snapshot.existing_size_bytes,
                snapshot.existing_sha256,
            )
            control_entries.append(entry)
            backup_entries[operation.operation_id] = entry

        temporary_entries = self._inspect_temporaries(
            len(control_entries),
            root,
        )
        control_entries.extend(temporary_entries)

        payloads_by_id = {
            payload.operation_id: payload
            for payload in self.verification_result.payloads
        }
        base_records: list[
            tuple[
                ArtifactApplicationOperation,
                ArtifactPayload,
                ArtifactTransactionDestinationState,
                int | None,
                str | None,
                ArtifactTransactionBackupState,
                str | None,
                tuple[str, ...],
            ]
        ] = []
        destination_conflict = False

        for operation, snapshot in zip(
            self.application_plan.operations,
            self.preflight_result.snapshot,
            strict=True,
        ):
            payload = payloads_by_id.get(operation.operation_id)
            if payload is None:
                raise ArtifactTransactionReconciliationIntegrityError(
                    "payload boundary is incomplete"
                )
            (
                destination_state,
                current_size,
                current_hash,
                destination_reasons,
            ) = self._inspect_destination(
                root,
                operation,
                payload,
                snapshot.existing_size_bytes,
                snapshot.existing_sha256,
            )
            if (
                destination_state
                is ArtifactTransactionDestinationState.CONFLICTED
            ):
                destination_conflict = True

            backup_path = operation.backup_path
            if operation.operation is ArtifactOperation.CREATE:
                backup_state = (
                    ArtifactTransactionBackupState.NOT_APPLICABLE
                )
            else:
                backup_entry = backup_entries[operation.operation_id]
                if (
                    backup_entry.state
                    is ArtifactTransactionControlState.ABSENT
                ):
                    backup_state = ArtifactTransactionBackupState.ABSENT
                elif (
                    backup_entry.state
                    is ArtifactTransactionControlState.MATCHING
                ):
                    backup_state = ArtifactTransactionBackupState.VALID
                else:
                    backup_state = ArtifactTransactionBackupState.INVALID

            base_records.append(
                (
                    operation,
                    payload,
                    destination_state,
                    current_size,
                    current_hash,
                    backup_state,
                    backup_path,
                    destination_reasons,
                )
            )

        invalid_control = any(
            entry.state is ArtifactTransactionControlState.INVALID
            for entry in control_entries
        )
        receipt_valid = (
            receipt_entry.state
            is ArtifactTransactionControlState.MATCHING
            and receipt is not None
        )
        receipt_invalid = (
            receipt_entry.state
            is ArtifactTransactionControlState.INVALID
        )
        all_before = all(
            item[2] is ArtifactTransactionDestinationState.BEFORE
            for item in base_records
        )
        all_after = all(
            item[2] is ArtifactTransactionDestinationState.AFTER
            for item in base_records
        )
        residual_control = any(
            entry.state is ArtifactTransactionControlState.RESIDUAL
            for entry in control_entries
        ) or any(
            entry.kind is ArtifactTransactionControlKind.BACKUP
            and entry.state is ArtifactTransactionControlState.MATCHING
            for entry in control_entries
        )

        rollback_possible = all(
            state is ArtifactTransactionDestinationState.BEFORE
            or (
                operation.operation is ArtifactOperation.CREATE
                and state is ArtifactTransactionDestinationState.AFTER
            )
            or (
                operation.operation is ArtifactOperation.UPDATE
                and state is ArtifactTransactionDestinationState.AFTER
                and backup_state
                is ArtifactTransactionBackupState.VALID
            )
            for (
                operation,
                _payload,
                state,
                _current_size,
                _current_hash,
                backup_state,
                _backup_path,
                _reasons,
            ) in base_records
        )

        top_reasons: list[str] = []
        if destination_conflict:
            top_reasons.append(
                "CONFLICTED: at least one destination matches neither the "
                "preflight snapshot nor the verified payload"
            )
        if invalid_control:
            top_reasons.append(
                "CONFLICTED: at least one transaction control entry is invalid"
            )
        if receipt_valid and not all_after:
            top_reasons.append(
                "CONFLICTED: committed receipt exists but final destinations "
                "do not all match verified payloads"
            )

        if (
            destination_conflict
            or invalid_control
            or receipt_invalid
            or (receipt_valid and not all_after)
        ):
            status = ArtifactTransactionReconciliationStatus.CONFLICTED
            strategy = ArtifactTransactionRecoveryStrategy.MANUAL_REVIEW
        elif receipt_valid:
            status = ArtifactTransactionReconciliationStatus.COMMITTED
            strategy = (
                ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY
                if residual_control
                else ArtifactTransactionRecoveryStrategy.NONE
            )
            top_reasons.append(
                "COMMITTED: durable receipt and every final destination match"
            )
        elif all_before and not residual_control:
            status = ArtifactTransactionReconciliationStatus.CLEAN
            strategy = ArtifactTransactionRecoveryStrategy.NONE
            top_reasons.append(
                "CLEAN: workspace and control state match the pre-transaction snapshot"
            )
        elif all_before:
            status = ArtifactTransactionReconciliationStatus.RECOVERABLE
            strategy = ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY
            top_reasons.append(
                "RECOVERABLE: no artifact change remains; only residual "
                "transaction controls require cleanup"
            )
        elif all_after and self.policy.allow_finalize_without_receipt:
            status = ArtifactTransactionReconciliationStatus.RECOVERABLE
            strategy = ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT
            top_reasons.append(
                "RECOVERABLE: every destination matches the verified payload "
                "but the durable committed receipt is absent"
            )
        elif rollback_possible:
            status = ArtifactTransactionReconciliationStatus.RECOVERABLE
            strategy = ArtifactTransactionRecoveryStrategy.ROLLBACK
            top_reasons.append(
                "RECOVERABLE: partially applied transaction can be returned "
                "to the preflight snapshot"
            )
        else:
            status = ArtifactTransactionReconciliationStatus.CONFLICTED
            strategy = ArtifactTransactionRecoveryStrategy.MANUAL_REVIEW
            top_reasons.append(
                "CONFLICTED: no safe deterministic recovery strategy exists"
            )

        records = tuple(
            self._build_record(
                item,
                strategy,
                status,
            )
            for item in base_records
        )
        control_actions = self._build_control_actions(
            tuple(control_entries),
            strategy,
            status,
            receipt_entry.relative_path,
        )

        request_hash = self.request.request_hash
        assert request_hash is not None
        return ArtifactTransactionReconciliationResult(
            reconciliation_id=self.request.reconciliation_id,
            status=status,
            strategy=strategy,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            transaction_id=self.transaction_request.transaction_id,
            transaction_request_hash=(
                self.request.transaction_request_hash
            ),
            transaction_policy_id=self.transaction_policy.policy_id,
            transaction_policy_hash=self.transaction_policy.policy_hash,
            preflight_id=self.preflight_result.preflight_id,
            preflight_result_hash=self.request.preflight_result_hash,
            snapshot_hash=self.preflight_result.snapshot_hash,
            verification_id=self.verification_result.verification_id,
            verification_result_hash=(
                self.request.verification_result_hash
            ),
            payload_manifest_hash=(
                self.verification_result.payload_manifest_hash
            ),
            application_id=self.application_plan.application_id,
            application_plan_hash=self.request.application_plan_hash,
            plan_id=self.application_plan.plan_id,
            step_id=self.application_plan.step_id,
            agent_id=self.application_plan.agent_id,
            workspace_root=root_text,
            control_entries=tuple(control_entries),
            records=records,
            control_actions=control_actions,
            top_level_reasons=tuple(top_reasons),
            before_count=sum(
                item.destination_state
                is ArtifactTransactionDestinationState.BEFORE
                for item in records
            ),
            after_count=sum(
                item.destination_state
                is ArtifactTransactionDestinationState.AFTER
                for item in records
            ),
            conflicted_count=sum(
                item.destination_state
                is ArtifactTransactionDestinationState.CONFLICTED
                for item in records
            ),
            inspected_at=self.request.requested_at,
        )

    def _path(
        self,
        root: Path,
        relative_path: str,
    ) -> Path:
        path = root.joinpath(
            *PurePosixPath(relative_path).parts
        )
        _assert_inside_root(root, path)
        return path

    def _control_file_values(
        self,
        path: Path,
    ) -> tuple[
        ArtifactWorkspaceEntryType,
        int | None,
        str | None,
    ]:
        entry_type = _classify_lstat(path)
        if entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ArtifactTransactionReconciliationError(
                    f"cannot stat control file: {path}"
                ) from exc
            return entry_type, size, _file_sha256(path)
        return entry_type, None, None

    def _inspect_lock(
        self,
        index: int,
        root: Path,
    ) -> ArtifactTransactionControlEntry:
        relative_path = self.transaction_policy.lock_relative_path
        path = self._path(root, relative_path)
        entry_type, size, digest = self._control_file_values(path)
        if entry_type is ArtifactWorkspaceEntryType.ABSENT:
            state = ArtifactTransactionControlState.ABSENT
            reason = "ABSENT: transaction lock is not present"
        elif entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
            try:
                data = json.loads(
                    _read_limited_text(
                        path,
                        self.policy.max_control_file_bytes,
                    )
                )
                if (
                    isinstance(data, dict)
                    and data.get("transaction_id")
                    == self.transaction_request.transaction_id
                    and data.get("request_hash")
                    == self.transaction_request.request_hash
                ):
                    state = ArtifactTransactionControlState.RESIDUAL
                    reason = (
                        "RESIDUAL: lock matches the interrupted transaction"
                    )
                else:
                    state = ArtifactTransactionControlState.INVALID
                    reason = (
                        "INVALID: lock belongs to another or malformed transaction"
                    )
            except (
                json.JSONDecodeError,
                ArtifactTransactionReconciliationError,
            ):
                state = ArtifactTransactionControlState.INVALID
                reason = "INVALID: lock content cannot be validated"
        else:
            state = ArtifactTransactionControlState.INVALID
            reason = "INVALID: lock path is not a regular file"
        return ArtifactTransactionControlEntry(
            index=index,
            relative_path=relative_path,
            kind=ArtifactTransactionControlKind.LOCK,
            entry_type=entry_type,
            state=state,
            size_bytes=size,
            sha256=digest,
            reason=reason,
        )

    def _inspect_receipt(
        self,
        index: int,
        root: Path,
    ) -> tuple[
        ArtifactTransactionControlEntry,
        ArtifactTransactionResult | None,
    ]:
        relative_path = self.transaction_policy.receipt_relative_path(
            self.transaction_request.transaction_id
        )
        path = self._path(root, relative_path)
        entry_type, size, digest = self._control_file_values(path)
        receipt: ArtifactTransactionResult | None = None
        if entry_type is ArtifactWorkspaceEntryType.ABSENT:
            state = ArtifactTransactionControlState.ABSENT
            reason = "ABSENT: durable transaction receipt is not present"
        elif entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
            try:
                receipt = ArtifactTransactionResult.from_json(
                    _read_limited_text(
                        path,
                        self.policy.max_control_file_bytes,
                    ).strip()
                )
                receipt.verify_hash()
                request_hash = self.transaction_request.request_hash
                assert request_hash is not None
                if (
                    receipt.status
                    is ArtifactTransactionStatus.COMMITTED
                    and receipt.transaction_id
                    == self.transaction_request.transaction_id
                    and receipt.request_hash == request_hash
                    and receipt.receipt_path == relative_path
                    and receipt.application_plan_hash
                    == self.application_plan.plan_hash
                    and receipt.verification_result_hash
                    == self.verification_result.result_hash
                    and receipt.snapshot_hash
                    == self.preflight_result.snapshot_hash
                ):
                    state = ArtifactTransactionControlState.MATCHING
                    reason = (
                        "MATCHING: committed receipt is valid and bound to "
                        "the transaction"
                    )
                else:
                    receipt = None
                    state = ArtifactTransactionControlState.INVALID
                    reason = (
                        "INVALID: receipt does not match the transaction boundary"
                    )
            except (
                ArtifactTransactionReconciliationError,
                ArtifactTransactionError,
                ValueError,
            ):
                receipt = None
                state = ArtifactTransactionControlState.INVALID
                reason = "INVALID: receipt content or integrity is invalid"
        else:
            state = ArtifactTransactionControlState.INVALID
            reason = "INVALID: receipt path is not a regular file"
        return (
            ArtifactTransactionControlEntry(
                index=index,
                relative_path=relative_path,
                kind=ArtifactTransactionControlKind.RECEIPT,
                entry_type=entry_type,
                state=state,
                size_bytes=size,
                sha256=digest,
                reason=reason,
            ),
            receipt,
        )

    def _inspect_backup(
        self,
        index: int,
        root: Path,
        operation: ArtifactApplicationOperation,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> ArtifactTransactionControlEntry:
        if operation.backup_path is None:
            raise ArtifactTransactionReconciliationIntegrityError(
                "update operation is missing backup_path"
            )
        relative_path = operation.backup_path
        path = self._path(root, relative_path)
        entry_type, size, digest = self._control_file_values(path)
        if entry_type is ArtifactWorkspaceEntryType.ABSENT:
            state = ArtifactTransactionControlState.ABSENT
            reason = "ABSENT: update backup is not present"
        elif (
            entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE
            and expected_size is not None
            and expected_sha256 is not None
            and size == expected_size
            and digest == expected_sha256
        ):
            state = ArtifactTransactionControlState.MATCHING
            reason = (
                "MATCHING: backup matches the preflight snapshot"
            )
        else:
            state = ArtifactTransactionControlState.INVALID
            reason = (
                "INVALID: backup is not a regular file matching the snapshot"
            )
        return ArtifactTransactionControlEntry(
            index=index,
            relative_path=relative_path,
            kind=ArtifactTransactionControlKind.BACKUP,
            entry_type=entry_type,
            state=state,
            size_bytes=size,
            sha256=digest,
            reason=reason,
        )

    def _inspect_temporaries(
        self,
        start_index: int,
        root: Path,
    ) -> tuple[ArtifactTransactionControlEntry, ...]:
        directories: set[Path] = set()
        directories.add(
            self._path(
                root,
                self.transaction_policy.receipt_root,
            )
        )
        directories.add(
            self._path(
                root,
                self.transaction_policy.lock_relative_path,
            ).parent
        )
        for operation in self.application_plan.operations:
            directories.add(
                self._path(root, operation.destination_path).parent
            )
            if operation.backup_path is not None:
                directories.add(
                    self._path(root, operation.backup_path).parent
                )

        candidates: set[Path] = set()
        prefixes = (
            ".elman-write-",
            ".elman-backup-",
            ".elman-receipt-",
        )
        for directory in sorted(
            directories,
            key=lambda item: item.as_posix(),
        ):
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                entries = tuple(directory.iterdir())
            except OSError as exc:
                raise ArtifactTransactionReconciliationError(
                    f"cannot inspect temporary directory: {directory}"
                ) from exc
            for entry in entries:
                if (
                    entry.name.startswith(prefixes)
                    and entry.name.endswith(".tmp")
                ):
                    candidates.add(entry)

        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: item.relative_to(root).as_posix(),
            )
        )
        if len(ordered) > self.policy.max_temporary_entries:
            raise ArtifactTransactionReconciliationError(
                "temporary entry count exceeds reconciliation policy"
            )

        result: list[ArtifactTransactionControlEntry] = []
        for offset, path in enumerate(ordered):
            relative_path = path.relative_to(root).as_posix()
            entry_type, size, digest = self._control_file_values(path)
            if entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
                state = ArtifactTransactionControlState.RESIDUAL
                reason = (
                    "RESIDUAL: abandoned transaction temporary file detected"
                )
            else:
                state = ArtifactTransactionControlState.INVALID
                reason = (
                    "INVALID: transaction temporary path is not a regular file"
                )
            result.append(
                ArtifactTransactionControlEntry(
                    index=start_index + offset,
                    relative_path=relative_path,
                    kind=ArtifactTransactionControlKind.TEMPORARY,
                    entry_type=entry_type,
                    state=state,
                    size_bytes=size,
                    sha256=digest,
                    reason=reason,
                )
            )
        return tuple(result)

    def _inspect_destination(
        self,
        root: Path,
        operation: ArtifactApplicationOperation,
        payload: ArtifactPayload,
        before_size: int | None,
        before_sha256: str | None,
    ) -> tuple[
        ArtifactTransactionDestinationState,
        int | None,
        str | None,
        tuple[str, ...],
    ]:
        path = self._path(root, operation.destination_path)
        if _existing_prefix_symlinks(
            root,
            operation.destination_path,
        ):
            return (
                ArtifactTransactionDestinationState.CONFLICTED,
                None,
                None,
                (
                    "CONFLICTED: symbolic link detected in destination path",
                ),
            )
        if _case_conflicts(root, operation.destination_path):
            return (
                ArtifactTransactionDestinationState.CONFLICTED,
                None,
                None,
                (
                    "CONFLICTED: case-conflicting destination exists",
                ),
            )
        entry_type = _classify_lstat(path)
        if entry_type is ArtifactWorkspaceEntryType.ABSENT:
            if operation.operation is ArtifactOperation.CREATE:
                return (
                    ArtifactTransactionDestinationState.BEFORE,
                    None,
                    None,
                    (
                        "BEFORE: create destination remains absent",
                    ),
                )
            return (
                ArtifactTransactionDestinationState.CONFLICTED,
                None,
                None,
                (
                    "CONFLICTED: update destination is absent",
                ),
            )
        if entry_type is not ArtifactWorkspaceEntryType.REGULAR_FILE:
            return (
                ArtifactTransactionDestinationState.CONFLICTED,
                None,
                None,
                (
                    "CONFLICTED: destination is not a regular file",
                ),
            )
        try:
            current_size = path.stat().st_size
        except OSError as exc:
            raise ArtifactTransactionReconciliationError(
                f"cannot stat destination: {path}"
            ) from exc
        current_hash = _file_sha256(path)
        if (
            current_size == payload.size_bytes
            and current_hash == payload.content_sha256
        ):
            return (
                ArtifactTransactionDestinationState.AFTER,
                current_size,
                current_hash,
                (
                    "AFTER: destination matches the verified payload",
                ),
            )
        if (
            operation.operation is ArtifactOperation.UPDATE
            and before_size is not None
            and before_sha256 is not None
            and current_size == before_size
            and current_hash == before_sha256
        ):
            return (
                ArtifactTransactionDestinationState.BEFORE,
                current_size,
                current_hash,
                (
                    "BEFORE: destination matches the preflight snapshot",
                ),
            )
        return (
            ArtifactTransactionDestinationState.CONFLICTED,
            current_size,
            current_hash,
            (
                "CONFLICTED: destination matches neither before nor after state",
            ),
        )

    def _build_record(
        self,
        item: tuple[
            ArtifactApplicationOperation,
            ArtifactPayload,
            ArtifactTransactionDestinationState,
            int | None,
            str | None,
            ArtifactTransactionBackupState,
            str | None,
            tuple[str, ...],
        ],
        strategy: ArtifactTransactionRecoveryStrategy,
        status: ArtifactTransactionReconciliationStatus,
    ) -> ArtifactTransactionReconciliationRecord:
        (
            operation,
            _payload,
            destination_state,
            current_size,
            current_hash,
            backup_state,
            backup_path,
            reasons,
        ) = item
        action = ArtifactTransactionRecoveryAction.NONE
        action_reasons = list(reasons)

        if status is ArtifactTransactionReconciliationStatus.CONFLICTED:
            if (
                destination_state
                is ArtifactTransactionDestinationState.CONFLICTED
                or backup_state
                is ArtifactTransactionBackupState.INVALID
            ):
                action = ArtifactTransactionRecoveryAction.INVESTIGATE
                action_reasons.append(
                    "ACTION: manual investigation is required"
                )
        elif strategy is ArtifactTransactionRecoveryStrategy.ROLLBACK:
            if (
                destination_state
                is ArtifactTransactionDestinationState.AFTER
            ):
                if operation.operation is ArtifactOperation.CREATE:
                    action = (
                        ArtifactTransactionRecoveryAction.DELETE_CREATED_DESTINATION
                    )
                    action_reasons.append(
                        "ACTION: delete created destination after hash revalidation"
                    )
                else:
                    action = ArtifactTransactionRecoveryAction.RESTORE_BACKUP
                    action_reasons.append(
                        "ACTION: restore verified backup after hash revalidation"
                    )
        elif (
            strategy
            is ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT
        ):
            action = ArtifactTransactionRecoveryAction.FINALIZE_COMMIT
            action_reasons.append(
                "ACTION: include operation in a durable committed receipt"
            )

        return ArtifactTransactionReconciliationRecord(
            sequence=operation.sequence,
            operation_id=operation.operation_id,
            destination_path=operation.destination_path,
            operation=operation.operation,
            destination_state=destination_state,
            current_size_bytes=current_size,
            current_sha256=current_hash,
            backup_state=backup_state,
            backup_path=backup_path,
            action=action,
            reasons=tuple(action_reasons),
        )

    def _build_control_actions(
        self,
        entries: tuple[ArtifactTransactionControlEntry, ...],
        strategy: ArtifactTransactionRecoveryStrategy,
        status: ArtifactTransactionReconciliationStatus,
        receipt_path: str,
    ) -> tuple[str, ...]:
        actions: list[str] = []
        if status is ArtifactTransactionReconciliationStatus.CONFLICTED:
            actions.extend(
                f"MANUAL_REVIEW:{entry.relative_path}"
                for entry in entries
                if entry.state is ArtifactTransactionControlState.INVALID
            )
            return tuple(actions)

        if (
            strategy
            is ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT
        ):
            actions.append(f"WRITE_COMMITTED_RECEIPT:{receipt_path}")

        for entry in entries:
            if (
                entry.kind is ArtifactTransactionControlKind.LOCK
                and entry.state
                is ArtifactTransactionControlState.RESIDUAL
                and self.policy.plan_remove_residual_lock
            ):
                actions.append(
                    f"REMOVE_RESIDUAL_LOCK:{entry.relative_path}"
                )
            elif (
                entry.kind
                is ArtifactTransactionControlKind.TEMPORARY
                and entry.state
                is ArtifactTransactionControlState.RESIDUAL
                and self.policy.plan_remove_temporary_files
            ):
                actions.append(
                    f"REMOVE_TEMPORARY:{entry.relative_path}"
                )
            elif (
                entry.kind is ArtifactTransactionControlKind.BACKUP
                and entry.state
                is ArtifactTransactionControlState.MATCHING
                and strategy
                in {
                    ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY,
                    ArtifactTransactionRecoveryStrategy.ROLLBACK,
                }
                and self.policy.plan_remove_valid_backups_after_rollback
            ):
                actions.append(
                    f"REMOVE_VALID_BACKUP_AFTER_RECOVERY:{entry.relative_path}"
                )
        return tuple(actions)
