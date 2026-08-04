"""Read-only workspace preflight inspection for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .agent_contracts import canonical_json
from .agent_output_validation import ArtifactClassification, ArtifactOperation
from .artifact_application_plan import (
    ArtifactApplicationDecision,
    ArtifactApplicationOperation,
    ArtifactApplicationPlan,
)
from .artifact_payload_verification import (
    ArtifactPayloadVerificationResult,
    ArtifactPayloadVerificationStatus,
)


ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactWorkspacePreflightError(ValueError):
    """A workspace preflight policy, request, snapshot, or result is invalid."""


class ArtifactWorkspacePreflightIntegrityError(
    ArtifactWorkspacePreflightError
):
    """A workspace preflight object fails an integrity check."""


class ArtifactWorkspacePreflightStatus(StrEnum):
    READY = "ready"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


class ArtifactWorkspaceEntryType(StrEnum):
    ABSENT = "absent"
    REGULAR_FILE = "regular-file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class ArtifactWorkspaceRecordDecision(StrEnum):
    READY = "ready"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactWorkspacePreflightError(
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
        raise ArtifactWorkspacePreflightError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactWorkspacePreflightError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ArtifactWorkspacePreflightError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ArtifactWorkspacePreflightError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactWorkspacePreflightError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactWorkspacePreflightError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactWorkspacePreflightError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactWorkspacePreflightError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactWorkspacePreflightError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactWorkspacePreflightError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactWorkspacePreflightError(
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
        raise ArtifactWorkspacePreflightError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactWorkspacePreflightError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactWorkspacePreflightError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactWorkspacePreflightError(
                f"{name} contains a non-portable segment"
            )
    return path


def _string_tuple(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ArtifactWorkspacePreflightError(
            f"{name} must be an iterable"
        )
    try:
        raw = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ArtifactWorkspacePreflightError(
            f"{name} must be an iterable"
        ) from exc
    return tuple(
        sorted(
            {
                _text(item, name).lower()
                for item in raw
            }
        )
    )


def _normalize_workspace_root(
    value: str | os.PathLike[str],
    *,
    reject_symlink_root: bool,
) -> str:
    path = Path(value).expanduser()
    if not path.exists():
        raise ArtifactWorkspacePreflightError(
            "workspace_root must exist"
        )
    if reject_symlink_root and path.is_symlink():
        raise ArtifactWorkspacePreflightError(
            "workspace_root cannot be a symbolic link"
        )
    if not path.is_dir():
        raise ArtifactWorkspacePreflightError(
            "workspace_root must be a directory"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactWorkspacePreflightError(
            "workspace_root cannot be resolved"
        ) from exc
    return resolved.as_posix()


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
        raise ArtifactWorkspacePreflightError(
            f"cannot read existing file: {path}"
        ) from exc
    return digest.hexdigest()


def _relative_parent(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent == "." else parent


def _assert_inside_root(root: Path, candidate: Path) -> None:
    try:
        common = os.path.commonpath(
            [str(root), str(candidate.resolve(strict=False))]
        )
    except (OSError, ValueError) as exc:
        raise ArtifactWorkspacePreflightError(
            "candidate path cannot be resolved safely"
        ) from exc
    if Path(common) != root:
        raise ArtifactWorkspacePreflightError(
            "candidate path escapes workspace root"
        )


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


def _nearest_existing_parent(path: Path, root: Path) -> Path:
    current = path
    while current != root and not current.exists():
        current = current.parent
    return current


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
        raise ArtifactWorkspacePreflightError(
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


@dataclass(frozen=True, slots=True)
class ArtifactWorkspacePreflightPolicy:
    policy_id: str
    max_operations: int = 64
    max_existing_file_bytes: int = 100_000_000
    review_existing_file_bytes: int = 10_000_000
    require_writable_parent: bool = True
    require_existing_parent_for_create: bool = True
    reject_symlinks: bool = True
    reject_symlink_root: bool = True
    require_rollback_availability: bool = True
    review_classifications: tuple[str, ...] = ()
    version: int = ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "max_operations",
            "max_existing_file_bytes",
            "review_existing_file_bytes",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        if (
            self.review_existing_file_bytes
            > self.max_existing_file_bytes
        ):
            raise ArtifactWorkspacePreflightError(
                "review_existing_file_bytes cannot exceed max_existing_file_bytes"
            )
        for field_name in (
            "require_writable_parent",
            "require_existing_parent_for_create",
            "reject_symlinks",
            "reject_symlink_root",
            "require_rollback_availability",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(
                    getattr(self, field_name),
                    field_name,
                ),
            )

        classifications = _string_tuple(
            self.review_classifications,
            "review_classifications",
        )
        valid = {
            item.value
            for item in ArtifactClassification
        }
        if not set(classifications).issubset(valid):
            raise ArtifactWorkspacePreflightError(
                "review_classifications contains an unknown value"
            )
        object.__setattr__(
            self,
            "review_classifications",
            classifications,
        )

        if self.version != ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION:
            raise ArtifactWorkspacePreflightError(
                "unsupported workspace preflight format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_workspace_preflight_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "max_operations": self.max_operations,
            "max_existing_file_bytes": (
                self.max_existing_file_bytes
            ),
            "review_existing_file_bytes": (
                self.review_existing_file_bytes
            ),
            "require_writable_parent": (
                self.require_writable_parent
            ),
            "require_existing_parent_for_create": (
                self.require_existing_parent_for_create
            ),
            "reject_symlinks": self.reject_symlinks,
            "reject_symlink_root": self.reject_symlink_root,
            "require_rollback_availability": (
                self.require_rollback_availability
            ),
            "review_classifications": list(
                self.review_classifications
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
    ) -> "ArtifactWorkspacePreflightPolicy":
        if (
            data.get("record_type")
            != "artifact_workspace_preflight_policy"
        ):
            raise ArtifactWorkspacePreflightError(
                "record_type must be artifact_workspace_preflight_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            max_operations=data["max_operations"],
            max_existing_file_bytes=data[
                "max_existing_file_bytes"
            ],
            review_existing_file_bytes=data[
                "review_existing_file_bytes"
            ],
            require_writable_parent=data[
                "require_writable_parent"
            ],
            require_existing_parent_for_create=data[
                "require_existing_parent_for_create"
            ],
            reject_symlinks=data["reject_symlinks"],
            reject_symlink_root=data["reject_symlink_root"],
            require_rollback_availability=data[
                "require_rollback_availability"
            ],
            review_classifications=tuple(
                data["review_classifications"]
            ),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactWorkspacePreflightPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactWorkspacePreflightError(
                "workspace preflight policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactWorkspacePreflightError(
                "workspace preflight policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactWorkspacePreflightRequest:
    preflight_id: str
    policy_id: str
    policy_hash: str
    verification_id: str
    verification_result_hash: str
    application_id: str
    application_plan_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "preflight_id",
            "policy_id",
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
            "verification_result_hash",
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
            raise ArtifactWorkspacePreflightError(
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
            _utc_timestamp(self.requested_at, "requested_at"),
        )

        if self.version != ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION:
            raise ArtifactWorkspacePreflightError(
                "unsupported workspace preflight format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactWorkspacePreflightIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        application_plan: ArtifactApplicationPlan,
        verification_result: ArtifactPayloadVerificationResult,
        policy: ArtifactWorkspacePreflightPolicy,
        *,
        workspace_root: str | os.PathLike[str],
        requested_by: str,
        requested_at: str | datetime,
        preflight_id: str | None = None,
    ) -> "ArtifactWorkspacePreflightRequest":
        if not isinstance(application_plan, ArtifactApplicationPlan):
            raise ArtifactWorkspacePreflightError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactWorkspacePreflightError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(policy, ArtifactWorkspacePreflightPolicy):
            raise ArtifactWorkspacePreflightError(
                "policy must be an ArtifactWorkspacePreflightPolicy"
            )
        application_plan.verify_hash()
        verification_result.verify_hash()
        plan_hash = application_plan.plan_hash
        result_hash = verification_result.result_hash
        assert plan_hash is not None
        assert result_hash is not None

        normalized_root = _normalize_workspace_root(
            workspace_root,
            reject_symlink_root=policy.reject_symlink_root,
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
        source_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_workspace_preflight_request_source"
                ),
                "policy_hash": policy.policy_hash,
                "verification_id": (
                    verification_result.verification_id
                ),
                "verification_result_hash": result_hash,
                "application_id": application_plan.application_id,
                "application_plan_hash": plan_hash,
                "plan_id": application_plan.plan_id,
                "step_id": application_plan.step_id,
                "agent_id": application_plan.agent_id,
                "workspace_root": normalized_root,
                "requested_by": normalized_requester,
                "requested_at": normalized_time,
            }
        )
        effective_id = (
            preflight_id
            if preflight_id is not None
            else f"workspace-preflight:{source_hash}"
        )

        return cls(
            preflight_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            verification_id=verification_result.verification_id,
            verification_result_hash=result_hash,
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
            "record_type": "artifact_workspace_preflight_request",
            "version": self.version,
            "preflight_id": self.preflight_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": (
                self.verification_result_hash
            ),
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
            raise ArtifactWorkspacePreflightIntegrityError(
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
    ) -> "ArtifactWorkspacePreflightRequest":
        if (
            data.get("record_type")
            != "artifact_workspace_preflight_request"
        ):
            raise ArtifactWorkspacePreflightError(
                "record_type must be artifact_workspace_preflight_request"
            )
        if "request_hash" not in data:
            raise ArtifactWorkspacePreflightIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            preflight_id=data["preflight_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data[
                "verification_result_hash"
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
    ) -> "ArtifactWorkspacePreflightRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactWorkspacePreflightError(
                "workspace preflight request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactWorkspacePreflightError(
                "workspace preflight request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactWorkspaceSnapshotEntry:
    sequence: int
    operation_id: str
    destination_path: str
    operation: ArtifactOperation
    classification: ArtifactClassification
    entry_type: ArtifactWorkspaceEntryType
    destination_exists: bool
    existing_size_bytes: int | None
    existing_sha256: str | None
    parent_path: str
    parent_exists: bool
    parent_writable: bool
    rollback_path: str | None
    rollback_parent_path: str | None
    rollback_available: bool
    symlink_paths: tuple[str, ...]
    case_conflicts: tuple[str, ...]
    entry_hash: str | None = None

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
            raise ArtifactWorkspacePreflightError(
                "operation is invalid"
            ) from exc
        object.__setattr__(self, "operation", operation)
        try:
            classification = ArtifactClassification(
                self.classification
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactWorkspacePreflightError(
                "classification is invalid"
            ) from exc
        object.__setattr__(
            self,
            "classification",
            classification,
        )
        try:
            entry_type = ArtifactWorkspaceEntryType(
                self.entry_type
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactWorkspacePreflightError(
                "entry_type is invalid"
            ) from exc
        object.__setattr__(self, "entry_type", entry_type)
        for field_name in (
            "destination_exists",
            "parent_exists",
            "parent_writable",
            "rollback_available",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        if self.existing_size_bytes is not None:
            object.__setattr__(
                self,
                "existing_size_bytes",
                _non_negative_int(
                    self.existing_size_bytes,
                    "existing_size_bytes",
                ),
            )
        if self.existing_sha256 is not None:
            object.__setattr__(
                self,
                "existing_sha256",
                _hash(
                    self.existing_sha256,
                    "existing_sha256",
                ),
            )
        if (
            entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE
            and (
                self.existing_size_bytes is None
                or self.existing_sha256 is None
            )
        ):
            raise ArtifactWorkspacePreflightError(
                "regular file snapshot requires size and sha256"
            )
        if (
            entry_type is not ArtifactWorkspaceEntryType.REGULAR_FILE
            and (
                self.existing_size_bytes is not None
                or self.existing_sha256 is not None
            )
        ):
            raise ArtifactWorkspacePreflightError(
                "non-file snapshot cannot contain file size or sha256"
            )
        object.__setattr__(
            self,
            "parent_path",
            _portable_relative_path(
                self.parent_path,
                "parent_path",
            )
            if self.parent_path != "."
            else ".",
        )
        if self.rollback_path is not None:
            object.__setattr__(
                self,
                "rollback_path",
                _portable_relative_path(
                    self.rollback_path,
                    "rollback_path",
                ),
            )
        if self.rollback_parent_path is not None:
            object.__setattr__(
                self,
                "rollback_parent_path",
                _portable_relative_path(
                    self.rollback_parent_path,
                    "rollback_parent_path",
                )
                if self.rollback_parent_path != "."
                else ".",
            )
        symlink_paths = tuple(
            sorted(
                {
                    _portable_relative_path(
                        item,
                        "symlink_path",
                    )
                    for item in self.symlink_paths
                }
            )
        )
        object.__setattr__(
            self,
            "symlink_paths",
            symlink_paths,
        )
        case_conflicts = tuple(
            sorted(
                {
                    _text(item, "case_conflict")
                    for item in self.case_conflicts
                }
            )
        )
        object.__setattr__(
            self,
            "case_conflicts",
            case_conflicts,
        )

        computed = self.compute_hash()
        if self.entry_hash is None:
            object.__setattr__(self, "entry_hash", computed)
        else:
            supplied = _hash(self.entry_hash, "entry_hash")
            if supplied != computed:
                raise ArtifactWorkspacePreflightIntegrityError(
                    "snapshot entry hash does not match content"
                )
            object.__setattr__(self, "entry_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "operation": self.operation.value,
            "classification": self.classification.value,
            "entry_type": self.entry_type.value,
            "destination_exists": self.destination_exists,
            "existing_size_bytes": self.existing_size_bytes,
            "existing_sha256": self.existing_sha256,
            "parent_path": self.parent_path,
            "parent_exists": self.parent_exists,
            "parent_writable": self.parent_writable,
            "rollback_path": self.rollback_path,
            "rollback_parent_path": self.rollback_parent_path,
            "rollback_available": self.rollback_available,
            "symlink_paths": list(self.symlink_paths),
            "case_conflicts": list(self.case_conflicts),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.entry_hash != self.compute_hash():
            raise ArtifactWorkspacePreflightIntegrityError(
                "snapshot entry hash does not match content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["entry_hash"] = self.entry_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactWorkspaceSnapshotEntry":
        return cls(
            sequence=data["sequence"],
            operation_id=data["operation_id"],
            destination_path=data["destination_path"],
            operation=ArtifactOperation(data["operation"]),
            classification=ArtifactClassification(
                data["classification"]
            ),
            entry_type=ArtifactWorkspaceEntryType(
                data["entry_type"]
            ),
            destination_exists=data["destination_exists"],
            existing_size_bytes=data.get(
                "existing_size_bytes"
            ),
            existing_sha256=data.get("existing_sha256"),
            parent_path=data["parent_path"],
            parent_exists=data["parent_exists"],
            parent_writable=data["parent_writable"],
            rollback_path=data.get("rollback_path"),
            rollback_parent_path=data.get(
                "rollback_parent_path"
            ),
            rollback_available=data["rollback_available"],
            symlink_paths=tuple(data["symlink_paths"]),
            case_conflicts=tuple(data["case_conflicts"]),
            entry_hash=data.get("entry_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactWorkspacePreflightRecord:
    index: int
    sequence: int
    operation_id: str
    destination_path: str
    decision: ArtifactWorkspaceRecordDecision
    snapshot_entry_hash: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
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
            decision = ArtifactWorkspaceRecordDecision(
                self.decision
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactWorkspacePreflightError(
                "record decision is invalid"
            ) from exc
        object.__setattr__(self, "decision", decision)
        object.__setattr__(
            self,
            "snapshot_entry_hash",
            _hash(
                self.snapshot_entry_hash,
                "snapshot_entry_hash",
            ),
        )
        reasons = tuple(
            dict.fromkeys(
                _text(item, "reason")
                for item in self.reasons
            )
        )
        if not reasons:
            raise ArtifactWorkspacePreflightError(
                "record must contain at least one reason"
            )
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "decision": self.decision.value,
            "snapshot_entry_hash": (
                self.snapshot_entry_hash
            ),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactWorkspacePreflightRecord":
        return cls(
            index=data["index"],
            sequence=data["sequence"],
            operation_id=data["operation_id"],
            destination_path=data["destination_path"],
            decision=ArtifactWorkspaceRecordDecision(
                data["decision"]
            ),
            snapshot_entry_hash=data[
                "snapshot_entry_hash"
            ],
            reasons=tuple(data["reasons"]),
        )


@dataclass(frozen=True, slots=True)
class ArtifactWorkspacePreflightResult:
    preflight_id: str
    status: ArtifactWorkspacePreflightStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    verification_id: str
    verification_result_hash: str
    application_id: str
    application_plan_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    snapshot: tuple[ArtifactWorkspaceSnapshotEntry, ...]
    records: tuple[ArtifactWorkspacePreflightRecord, ...]
    top_level_reasons: tuple[str, ...]
    ready_count: int
    review_count: int
    rejected_count: int
    snapshot_hash: str
    inspected_at: str
    result_hash: str | None = None
    version: int = ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "preflight_id",
            "policy_id",
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
            status_value = ArtifactWorkspacePreflightStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactWorkspacePreflightError(
                "preflight status is invalid"
            ) from exc
        object.__setattr__(self, "status", status_value)
        for field_name in (
            "request_hash",
            "policy_hash",
            "verification_result_hash",
            "application_plan_hash",
            "snapshot_hash",
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
            raise ArtifactWorkspacePreflightError(
                "workspace_root must be absolute"
            )
        object.__setattr__(
            self,
            "workspace_root",
            Path(workspace_root).as_posix(),
        )

        snapshot = tuple(self.snapshot)
        if not all(
            isinstance(item, ArtifactWorkspaceSnapshotEntry)
            for item in snapshot
        ):
            raise ArtifactWorkspacePreflightError(
                "snapshot must contain snapshot entries"
            )
        if tuple(item.sequence for item in snapshot) != tuple(
            range(1, len(snapshot) + 1)
        ):
            raise ArtifactWorkspacePreflightError(
                "snapshot sequences must be contiguous from one"
            )
        for item in snapshot:
            item.verify_hash()
        object.__setattr__(self, "snapshot", snapshot)

        computed_snapshot_hash = _sha256_document(
            {
                "record_type": "artifact_workspace_snapshot",
                "entries": [
                    item.to_dict()
                    for item in snapshot
                ],
            }
        )
        if self.snapshot_hash != computed_snapshot_hash:
            raise ArtifactWorkspacePreflightIntegrityError(
                "snapshot_hash does not match snapshot"
            )

        records = tuple(self.records)
        if not all(
            isinstance(item, ArtifactWorkspacePreflightRecord)
            for item in records
        ):
            raise ArtifactWorkspacePreflightError(
                "records must contain preflight records"
            )
        if tuple(item.index for item in records) != tuple(
            range(len(records))
        ):
            raise ArtifactWorkspacePreflightError(
                "record indexes must be contiguous from zero"
            )
        if len(records) != len(snapshot):
            raise ArtifactWorkspacePreflightIntegrityError(
                "records must cover every snapshot entry"
            )
        for entry, record in zip(snapshot, records, strict=True):
            if (
                entry.sequence != record.sequence
                or entry.operation_id != record.operation_id
                or entry.destination_path != record.destination_path
                or entry.entry_hash != record.snapshot_entry_hash
            ):
                raise ArtifactWorkspacePreflightIntegrityError(
                    "record does not match snapshot entry"
                )
        object.__setattr__(self, "records", records)

        reasons = tuple(
            dict.fromkeys(
                _text(item, "top_level_reason")
                for item in self.top_level_reasons
            )
        )
        object.__setattr__(
            self,
            "top_level_reasons",
            reasons,
        )
        for field_name in (
            "ready_count",
            "review_count",
            "rejected_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        actual_ready = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.READY
            for item in records
        )
        actual_review = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.REQUIRES_REVIEW
            for item in records
        )
        actual_rejected = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.REJECTED
            for item in records
        )
        if (
            self.ready_count,
            self.review_count,
            self.rejected_count,
        ) != (
            actual_ready,
            actual_review,
            actual_rejected,
        ):
            raise ArtifactWorkspacePreflightIntegrityError(
                "preflight counts do not match records"
            )

        expected_status = (
            ArtifactWorkspacePreflightStatus.REJECTED
            if actual_rejected
            or any(
                item.startswith("REJECTED:")
                for item in reasons
            )
            else ArtifactWorkspacePreflightStatus.REQUIRES_REVIEW
            if actual_review
            or any(
                item.startswith("REVIEW:")
                for item in reasons
            )
            else ArtifactWorkspacePreflightStatus.READY
        )
        if status_value is not expected_status:
            raise ArtifactWorkspacePreflightIntegrityError(
                "preflight status does not match records"
            )
        object.__setattr__(
            self,
            "inspected_at",
            _utc_timestamp(self.inspected_at, "inspected_at"),
        )

        if self.version != ARTIFACT_WORKSPACE_PREFLIGHT_FORMAT_VERSION:
            raise ArtifactWorkspacePreflightError(
                "unsupported workspace preflight format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactWorkspacePreflightIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_workspace_preflight_result",
            "version": self.version,
            "preflight_id": self.preflight_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": (
                self.verification_result_hash
            ),
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "snapshot": [
                item.to_dict()
                for item in self.snapshot
            ],
            "records": [
                item.to_dict()
                for item in self.records
            ],
            "top_level_reasons": list(
                self.top_level_reasons
            ),
            "ready_count": self.ready_count,
            "review_count": self.review_count,
            "rejected_count": self.rejected_count,
            "snapshot_hash": self.snapshot_hash,
            "inspected_at": self.inspected_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactWorkspacePreflightIntegrityError(
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
    ) -> "ArtifactWorkspacePreflightResult":
        if (
            data.get("record_type")
            != "artifact_workspace_preflight_result"
        ):
            raise ArtifactWorkspacePreflightError(
                "record_type must be artifact_workspace_preflight_result"
            )
        if "result_hash" not in data:
            raise ArtifactWorkspacePreflightIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            preflight_id=data["preflight_id"],
            status=ArtifactWorkspacePreflightStatus(
                data["status"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data[
                "verification_result_hash"
            ],
            application_id=data["application_id"],
            application_plan_hash=data[
                "application_plan_hash"
            ],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            workspace_root=data["workspace_root"],
            snapshot=tuple(
                ArtifactWorkspaceSnapshotEntry.from_dict(
                    item
                )
                for item in data["snapshot"]
            ),
            records=tuple(
                ArtifactWorkspacePreflightRecord.from_dict(
                    item
                )
                for item in data["records"]
            ),
            top_level_reasons=tuple(
                data["top_level_reasons"]
            ),
            ready_count=data["ready_count"],
            review_count=data["review_count"],
            rejected_count=data["rejected_count"],
            snapshot_hash=data["snapshot_hash"],
            inspected_at=data["inspected_at"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactWorkspacePreflightResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactWorkspacePreflightError(
                "workspace preflight result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactWorkspacePreflightError(
                "workspace preflight result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactWorkspacePreflight:
    request: ArtifactWorkspacePreflightRequest
    application_plan: ArtifactApplicationPlan
    verification_result: ArtifactPayloadVerificationResult
    policy: ArtifactWorkspacePreflightPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactWorkspacePreflightRequest,
        ):
            raise ArtifactWorkspacePreflightError(
                "request must be an ArtifactWorkspacePreflightRequest"
            )
        if not isinstance(
            self.application_plan,
            ArtifactApplicationPlan,
        ):
            raise ArtifactWorkspacePreflightError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            self.verification_result,
            ArtifactPayloadVerificationResult,
        ):
            raise ArtifactWorkspacePreflightError(
                "verification_result must be an ArtifactPayloadVerificationResult"
            )
        if not isinstance(
            self.policy,
            ArtifactWorkspacePreflightPolicy,
        ):
            raise ArtifactWorkspacePreflightError(
                "policy must be an ArtifactWorkspacePreflightPolicy"
            )

        self.request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()

        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        assert plan_hash is not None
        assert verification_hash is not None

        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "verification_id": (
                self.verification_result.verification_id
            ),
            "verification_result_hash": verification_hash,
            "application_id": (
                self.application_plan.application_id
            ),
            "application_plan_hash": plan_hash,
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactWorkspacePreflightError(
                    f"request {field_name} does not match preflight source"
                )

        if (
            self.verification_result.application_id
            != self.application_plan.application_id
            or self.verification_result.application_plan_hash
            != plan_hash
            or self.verification_result.plan_id
            != self.application_plan.plan_id
            or self.verification_result.step_id
            != self.application_plan.step_id
            or self.verification_result.agent_id
            != self.application_plan.agent_id
        ):
            raise ArtifactWorkspacePreflightError(
                "verification result does not match application plan"
            )

    def inspect(self) -> ArtifactWorkspacePreflightResult:
        self.request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()

        root_text = _normalize_workspace_root(
            self.request.workspace_root,
            reject_symlink_root=self.policy.reject_symlink_root,
        )
        if root_text != self.request.workspace_root:
            raise ArtifactWorkspacePreflightError(
                "workspace root changed after request creation"
            )
        root = Path(root_text)

        top_rejected: list[str] = []
        if (
            self.application_plan.decision
            is not ArtifactApplicationDecision.READY
        ):
            top_rejected.append(
                "application plan decision must be ready"
            )
        if (
            self.verification_result.status
            is not ArtifactPayloadVerificationStatus.VERIFIED
        ):
            top_rejected.append(
                "payload verification status must be verified"
            )
        if not self.application_plan.operations:
            top_rejected.append(
                "application plan contains no operations"
            )
        if (
            len(self.application_plan.operations)
            > self.policy.max_operations
        ):
            top_rejected.append(
                "operation count exceeds preflight policy maximum"
            )

        snapshots: list[ArtifactWorkspaceSnapshotEntry] = []
        decisions: list[
            tuple[
                ArtifactWorkspaceRecordDecision,
                tuple[str, ...],
            ]
        ] = []

        for operation in self.application_plan.operations:
            snapshot, decision, reasons = self._inspect_operation(
                root,
                operation,
            )
            snapshots.append(snapshot)
            decisions.append((decision, reasons))

        snapshot_tuple = tuple(snapshots)
        snapshot_hash = _sha256_document(
            {
                "record_type": "artifact_workspace_snapshot",
                "entries": [
                    item.to_dict()
                    for item in snapshot_tuple
                ],
            }
        )

        records = tuple(
            ArtifactWorkspacePreflightRecord(
                index=index,
                sequence=snapshot.sequence,
                operation_id=snapshot.operation_id,
                destination_path=snapshot.destination_path,
                decision=decision,
                snapshot_entry_hash=snapshot.entry_hash or "",
                reasons=reasons,
            )
            for index, (
                snapshot,
                (decision, reasons),
            ) in enumerate(
                zip(
                    snapshot_tuple,
                    decisions,
                    strict=True,
                )
            )
        )

        ready_count = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.READY
            for item in records
        )
        review_count = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.REQUIRES_REVIEW
            for item in records
        )
        rejected_count = sum(
            item.decision
            is ArtifactWorkspaceRecordDecision.REJECTED
            for item in records
        )

        top_level_reasons = tuple(
            f"REJECTED: {item}"
            for item in dict.fromkeys(top_rejected)
        )
        status_value = (
            ArtifactWorkspacePreflightStatus.REJECTED
            if rejected_count or top_rejected
            else ArtifactWorkspacePreflightStatus.REQUIRES_REVIEW
            if review_count
            else ArtifactWorkspacePreflightStatus.READY
        )

        request_hash = self.request.request_hash
        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        assert request_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None

        return ArtifactWorkspacePreflightResult(
            preflight_id=self.request.preflight_id,
            status=status_value,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            verification_id=(
                self.verification_result.verification_id
            ),
            verification_result_hash=verification_hash,
            application_id=self.application_plan.application_id,
            application_plan_hash=plan_hash,
            plan_id=self.application_plan.plan_id,
            step_id=self.application_plan.step_id,
            agent_id=self.application_plan.agent_id,
            workspace_root=root_text,
            snapshot=snapshot_tuple,
            records=records,
            top_level_reasons=top_level_reasons,
            ready_count=ready_count,
            review_count=review_count,
            rejected_count=rejected_count,
            snapshot_hash=snapshot_hash,
            inspected_at=self.request.requested_at,
        )

    def _inspect_operation(
        self,
        root: Path,
        operation: ArtifactApplicationOperation,
    ) -> tuple[
        ArtifactWorkspaceSnapshotEntry,
        ArtifactWorkspaceRecordDecision,
        tuple[str, ...],
    ]:
        destination = root.joinpath(
            *PurePosixPath(operation.destination_path).parts
        )
        _assert_inside_root(root, destination)

        parent_relative = _relative_parent(
            operation.destination_path
        )
        parent = (
            root
            if parent_relative == "."
            else root.joinpath(
                *PurePosixPath(parent_relative).parts
            )
        )
        entry_type = _classify_lstat(destination)
        exists = entry_type is not ArtifactWorkspaceEntryType.ABSENT
        parent_type = _classify_lstat(parent)
        parent_exists = (
            parent_type is ArtifactWorkspaceEntryType.DIRECTORY
        )
        parent_writable = bool(
            parent_exists
            and os.access(parent, os.W_OK)
        )
        symlink_paths = _existing_prefix_symlinks(
            root,
            operation.destination_path,
        )
        conflicts = _case_conflicts(
            root,
            operation.destination_path,
        )

        existing_size: int | None = None
        existing_sha256: str | None = None
        if entry_type is ArtifactWorkspaceEntryType.REGULAR_FILE:
            try:
                existing_size = destination.stat().st_size
            except OSError as exc:
                raise ArtifactWorkspacePreflightError(
                    f"cannot stat destination: {destination}"
                ) from exc
            existing_sha256 = _file_sha256(destination)

        rollback_path = operation.backup_path
        rollback_parent_relative: str | None = None
        rollback_available = True
        rollback_symlinks: tuple[str, ...] = ()
        if rollback_path is not None:
            rollback_destination = root.joinpath(
                *PurePosixPath(rollback_path).parts
            )
            _assert_inside_root(root, rollback_destination)
            rollback_parent_relative = _relative_parent(
                rollback_path
            )
            rollback_parent = (
                root
                if rollback_parent_relative == "."
                else root.joinpath(
                    *PurePosixPath(
                        rollback_parent_relative
                    ).parts
                )
            )
            rollback_symlinks = _existing_prefix_symlinks(
                root,
                rollback_parent_relative,
            )
            nearest = _nearest_existing_parent(
                rollback_parent,
                root,
            )
            nearest_type = _classify_lstat(nearest)
            rollback_available = bool(
                nearest_type
                is ArtifactWorkspaceEntryType.DIRECTORY
                and not nearest.is_symlink()
                and os.access(nearest, os.W_OK)
            )
            if (
                rollback_parent.exists()
                and _classify_lstat(rollback_parent)
                is not ArtifactWorkspaceEntryType.DIRECTORY
            ):
                rollback_available = False

        combined_symlinks = tuple(
            sorted(
                set(symlink_paths)
                | set(rollback_symlinks)
            )
        )

        rejected: list[str] = []
        review: list[str] = []

        if self.policy.reject_symlinks and combined_symlinks:
            rejected.append(
                "symbolic link detected in destination or rollback path"
            )
        if conflicts:
            rejected.append(
                "case-conflicting destination exists in parent directory"
            )
        if (
            self.policy.require_writable_parent
            and not parent_writable
        ):
            rejected.append(
                "destination parent is not an existing writable directory"
            )

        if operation.operation is ArtifactOperation.CREATE:
            if exists:
                rejected.append(
                    "create destination must be absent"
                )
            if (
                self.policy.require_existing_parent_for_create
                and not parent_exists
            ):
                rejected.append(
                    "create destination parent must already exist"
                )
        else:
            if not exists:
                rejected.append(
                    "update destination must exist"
                )
            elif (
                entry_type
                is not ArtifactWorkspaceEntryType.REGULAR_FILE
            ):
                rejected.append(
                    "update destination must be a regular file"
                )
            if (
                existing_size is not None
                and existing_size
                > self.policy.max_existing_file_bytes
            ):
                rejected.append(
                    "existing file exceeds preflight maximum size"
                )
            elif (
                existing_size is not None
                and existing_size
                > self.policy.review_existing_file_bytes
            ):
                review.append(
                    "existing file size requires human review"
                )

        if (
            self.policy.require_rollback_availability
            and operation.requires_backup
            and not rollback_available
        ):
            rejected.append(
                "rollback storage is not available"
            )
        if (
            operation.classification.value
            in self.policy.review_classifications
        ):
            review.append(
                "artifact classification requires human review"
            )

        snapshot = ArtifactWorkspaceSnapshotEntry(
            sequence=operation.sequence,
            operation_id=operation.operation_id,
            destination_path=operation.destination_path,
            operation=operation.operation,
            classification=operation.classification,
            entry_type=entry_type,
            destination_exists=exists,
            existing_size_bytes=existing_size,
            existing_sha256=existing_sha256,
            parent_path=parent_relative,
            parent_exists=parent_exists,
            parent_writable=parent_writable,
            rollback_path=rollback_path,
            rollback_parent_path=rollback_parent_relative,
            rollback_available=rollback_available,
            symlink_paths=combined_symlinks,
            case_conflicts=conflicts,
        )

        if rejected:
            decision = ArtifactWorkspaceRecordDecision.REJECTED
            reasons = tuple(
                f"REJECTED: {item}"
                for item in dict.fromkeys(rejected)
            )
        elif review:
            decision = (
                ArtifactWorkspaceRecordDecision.REQUIRES_REVIEW
            )
            reasons = tuple(
                f"REVIEW: {item}"
                for item in dict.fromkeys(review)
            )
        else:
            decision = ArtifactWorkspaceRecordDecision.READY
            reasons = (
                "READY: workspace preconditions satisfy the application operation",
            )

        return snapshot, decision, reasons
