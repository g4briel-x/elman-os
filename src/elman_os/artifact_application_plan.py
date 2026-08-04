"""Deterministic artifact application planning for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from .agent_contracts import canonical_json
from .agent_output_validation import (
    AgentOutputValidationResult,
    AgentOutputValidationStatus,
    ArtifactClassification,
    ArtifactOperation,
    ArtifactValidationDecision,
    ArtifactValidationRecord,
)


ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactApplicationPlanError(ValueError):
    """An artifact application policy, request, operation, or plan is invalid."""


class ArtifactApplicationPlanIntegrityError(ArtifactApplicationPlanError):
    """An artifact application object fails an integrity check."""


class ArtifactApplicationDecision(StrEnum):
    READY = "ready"
    REQUIRES_APPROVAL = "requires-approval"
    REJECTED = "rejected"


class ArtifactRollbackAction(StrEnum):
    DELETE_CREATED = "delete-created"
    RESTORE_BACKUP = "restore-backup"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactApplicationPlanError(
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
        raise ArtifactApplicationPlanError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactApplicationPlanError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactApplicationPlanError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactApplicationPlanError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactApplicationPlanError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactApplicationPlanError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactApplicationPlanError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactApplicationPlanError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _positive_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ArtifactApplicationPlanError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ArtifactApplicationPlanError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _portable_relative_path(value: object, name: str) -> str:
    path = _text(value, name)
    if path != path.strip():
        raise ArtifactApplicationPlanError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactApplicationPlanError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactApplicationPlanError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactApplicationPlanError(
                f"{name} contains a non-portable segment"
            )
    return path


def _optional_identifier(
    value: object,
    name: str,
) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


@dataclass(frozen=True, slots=True)
class ArtifactApplicationPolicy:
    policy_id: str
    max_operations: int = 64
    require_human_approval_for_updates: bool = True
    rollback_required: bool = True
    rollback_root: str = ".elman-os/rollback"
    allowed_classifications: tuple[str, ...] = (
        ArtifactClassification.SOURCE.value,
        ArtifactClassification.TEST.value,
        ArtifactClassification.DOCUMENTATION.value,
        ArtifactClassification.CONFIGURATION.value,
        ArtifactClassification.DATA.value,
        ArtifactClassification.REPORT.value,
    )
    version: int = ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "max_operations",
            _positive_int(self.max_operations, "max_operations"),
        )
        if not isinstance(
            self.require_human_approval_for_updates,
            bool,
        ):
            raise ArtifactApplicationPlanError(
                "require_human_approval_for_updates must be boolean"
            )
        if not isinstance(self.rollback_required, bool):
            raise ArtifactApplicationPlanError(
                "rollback_required must be boolean"
            )
        object.__setattr__(
            self,
            "rollback_root",
            _portable_relative_path(
                self.rollback_root,
                "rollback_root",
            ),
        )

        normalized = tuple(
            sorted(
                {
                    ArtifactClassification(item).value
                    for item in self.allowed_classifications
                }
            )
        )
        if not normalized:
            raise ArtifactApplicationPlanError(
                "allowed_classifications cannot be empty"
            )
        object.__setattr__(
            self,
            "allowed_classifications",
            normalized,
        )

        if self.version != ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION:
            raise ArtifactApplicationPlanError(
                "unsupported artifact application plan format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_application_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "max_operations": self.max_operations,
            "require_human_approval_for_updates": (
                self.require_human_approval_for_updates
            ),
            "rollback_required": self.rollback_required,
            "rollback_root": self.rollback_root,
            "allowed_classifications": list(
                self.allowed_classifications
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
    ) -> "ArtifactApplicationPolicy":
        if data.get("record_type") != "artifact_application_policy":
            raise ArtifactApplicationPlanError(
                "record_type must be artifact_application_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            max_operations=data["max_operations"],
            require_human_approval_for_updates=(
                data["require_human_approval_for_updates"]
            ),
            rollback_required=data["rollback_required"],
            rollback_root=data["rollback_root"],
            allowed_classifications=tuple(
                data["allowed_classifications"]
            ),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactApplicationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactApplicationPlanError(
                "artifact application policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactApplicationPlanError(
                "artifact application policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactApplicationRequest:
    application_id: str
    policy_id: str
    policy_hash: str
    validation_id: str
    validation_result_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    requested_by: str
    requested_at: str
    approval_reference: str | None
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    request_hash: str | None = None
    version: int = ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "application_id",
            "policy_id",
            "validation_id",
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
            "validation_result_hash",
            _hash(
                self.validation_result_hash,
                "validation_result_hash",
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
        object.__setattr__(
            self,
            "approval_reference",
            _optional_identifier(
                self.approval_reference,
                "approval_reference",
            ),
        )
        for field_name in (
            "plan_state_hash",
            "journal_head_hash",
            "journal_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "journal_event_count",
            _non_negative_int(
                self.journal_event_count,
                "journal_event_count",
            ),
        )

        if self.version != ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION:
            raise ArtifactApplicationPlanError(
                "unsupported artifact application plan format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactApplicationPlanIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_validation_result(
        cls,
        result: AgentOutputValidationResult,
        policy: ArtifactApplicationPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        approval_reference: str | None = None,
        application_id: str | None = None,
    ) -> "ArtifactApplicationRequest":
        if not isinstance(result, AgentOutputValidationResult):
            raise ArtifactApplicationPlanError(
                "result must be an AgentOutputValidationResult"
            )
        if not isinstance(policy, ArtifactApplicationPolicy):
            raise ArtifactApplicationPlanError(
                "policy must be an ArtifactApplicationPolicy"
            )
        result.verify_hash()
        result_hash = result.result_hash
        assert result_hash is not None
        normalized_time = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        normalized_requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_approval = _optional_identifier(
            approval_reference,
            "approval_reference",
        )

        source_hash = _sha256_document(
            {
                "record_type": "artifact_application_request_source",
                "policy_hash": policy.policy_hash,
                "validation_id": result.validation_id,
                "validation_result_hash": result_hash,
                "plan_id": result.plan_id,
                "step_id": result.step_id,
                "agent_id": result.agent_id,
                "requested_by": normalized_requester,
                "requested_at": normalized_time,
                "approval_reference": normalized_approval,
                "plan_state_hash": result.plan_state_hash,
                "journal_event_count": result.journal_event_count,
                "journal_head_hash": result.journal_head_hash,
                "journal_hash": result.journal_hash,
            }
        )
        effective_id = (
            application_id
            if application_id is not None
            else f"artifact-application:{source_hash}"
        )

        return cls(
            application_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            validation_id=result.validation_id,
            validation_result_hash=result_hash,
            plan_id=result.plan_id,
            step_id=result.step_id,
            agent_id=result.agent_id,
            requested_by=normalized_requester,
            requested_at=normalized_time,
            approval_reference=normalized_approval,
            plan_state_hash=result.plan_state_hash,
            journal_event_count=result.journal_event_count,
            journal_head_hash=result.journal_head_hash,
            journal_hash=result.journal_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_application_request",
            "version": self.version,
            "application_id": self.application_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "validation_id": self.validation_id,
            "validation_result_hash": self.validation_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "approval_reference": self.approval_reference,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactApplicationPlanIntegrityError(
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
    ) -> "ArtifactApplicationRequest":
        if data.get("record_type") != "artifact_application_request":
            raise ArtifactApplicationPlanError(
                "record_type must be artifact_application_request"
            )
        if "request_hash" not in data:
            raise ArtifactApplicationPlanIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            application_id=data["application_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            validation_id=data["validation_id"],
            validation_result_hash=data["validation_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            approval_reference=data.get("approval_reference"),
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactApplicationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactApplicationPlanError(
                "artifact application request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactApplicationPlanError(
                "artifact application request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactApplicationOperation:
    sequence: int
    operation_id: str
    artifact_index: int
    destination_path: str
    operation: ArtifactOperation
    classification: ArtifactClassification
    sha256: str
    size_bytes: int
    media_type: str
    precondition: str
    requires_backup: bool
    backup_path: str | None
    rollback_action: ArtifactRollbackAction
    approval_reference: str | None
    operation_hash: str | None = None

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
            "artifact_index",
            _non_negative_int(
                self.artifact_index,
                "artifact_index",
            ),
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
            raise ArtifactApplicationPlanError(
                "operation is invalid"
            ) from exc
        object.__setattr__(self, "operation", operation)
        try:
            classification = ArtifactClassification(
                self.classification
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactApplicationPlanError(
                "classification is invalid"
            ) from exc
        object.__setattr__(
            self,
            "classification",
            classification,
        )
        object.__setattr__(
            self,
            "sha256",
            _hash(self.sha256, "sha256"),
        )
        object.__setattr__(
            self,
            "size_bytes",
            _non_negative_int(self.size_bytes, "size_bytes"),
        )
        media_type = _text(self.media_type, "media_type")
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise ArtifactApplicationPlanError(
                "media_type is invalid"
            )
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(
            self,
            "precondition",
            _text(self.precondition, "precondition"),
        )
        if not isinstance(self.requires_backup, bool):
            raise ArtifactApplicationPlanError(
                "requires_backup must be boolean"
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
        try:
            rollback_action = ArtifactRollbackAction(
                self.rollback_action
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactApplicationPlanError(
                "rollback_action is invalid"
            ) from exc
        object.__setattr__(
            self,
            "rollback_action",
            rollback_action,
        )
        object.__setattr__(
            self,
            "approval_reference",
            _optional_identifier(
                self.approval_reference,
                "approval_reference",
            ),
        )

        if operation is ArtifactOperation.CREATE:
            if self.requires_backup or self.backup_path is not None:
                raise ArtifactApplicationPlanError(
                    "create operation cannot require a backup"
                )
            if rollback_action is not ArtifactRollbackAction.DELETE_CREATED:
                raise ArtifactApplicationPlanError(
                    "create rollback must delete the created artifact"
                )
        else:
            if not self.requires_backup or self.backup_path is None:
                raise ArtifactApplicationPlanError(
                    "update operation must require a backup path"
                )
            if rollback_action is not ArtifactRollbackAction.RESTORE_BACKUP:
                raise ArtifactApplicationPlanError(
                    "update rollback must restore the backup"
                )

        computed = self.compute_hash()
        if self.operation_hash is None:
            object.__setattr__(self, "operation_hash", computed)
        else:
            supplied = _hash(
                self.operation_hash,
                "operation_hash",
            )
            if supplied != computed:
                raise ArtifactApplicationPlanIntegrityError(
                    "operation hash does not match operation content"
                )
            object.__setattr__(self, "operation_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "artifact_index": self.artifact_index,
            "destination_path": self.destination_path,
            "operation": self.operation.value,
            "classification": self.classification.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "precondition": self.precondition,
            "requires_backup": self.requires_backup,
            "backup_path": self.backup_path,
            "rollback_action": self.rollback_action.value,
            "approval_reference": self.approval_reference,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.operation_hash != self.compute_hash():
            raise ArtifactApplicationPlanIntegrityError(
                "operation hash does not match operation content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["operation_hash"] = self.operation_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactApplicationOperation":
        return cls(
            sequence=data["sequence"],
            operation_id=data["operation_id"],
            artifact_index=data["artifact_index"],
            destination_path=data["destination_path"],
            operation=ArtifactOperation(data["operation"]),
            classification=ArtifactClassification(
                data["classification"]
            ),
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            media_type=data["media_type"],
            precondition=data["precondition"],
            requires_backup=data["requires_backup"],
            backup_path=data.get("backup_path"),
            rollback_action=ArtifactRollbackAction(
                data["rollback_action"]
            ),
            approval_reference=data.get("approval_reference"),
            operation_hash=data.get("operation_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRollbackEntry:
    sequence: int
    destination_path: str
    action: ArtifactRollbackAction
    backup_path: str | None
    artifact_sha256: str
    entry_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sequence",
            _positive_int(self.sequence, "sequence"),
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
            action = ArtifactRollbackAction(self.action)
        except (TypeError, ValueError) as exc:
            raise ArtifactApplicationPlanError(
                "rollback action is invalid"
            ) from exc
        object.__setattr__(self, "action", action)
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
            "artifact_sha256",
            _hash(self.artifact_sha256, "artifact_sha256"),
        )
        if (
            action is ArtifactRollbackAction.RESTORE_BACKUP
            and self.backup_path is None
        ):
            raise ArtifactApplicationPlanError(
                "restore-backup entry requires backup_path"
            )
        if (
            action is ArtifactRollbackAction.DELETE_CREATED
            and self.backup_path is not None
        ):
            raise ArtifactApplicationPlanError(
                "delete-created entry cannot contain backup_path"
            )

        computed = self.compute_hash()
        if self.entry_hash is None:
            object.__setattr__(self, "entry_hash", computed)
        else:
            supplied = _hash(self.entry_hash, "entry_hash")
            if supplied != computed:
                raise ArtifactApplicationPlanIntegrityError(
                    "rollback entry hash does not match content"
                )
            object.__setattr__(self, "entry_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "destination_path": self.destination_path,
            "action": self.action.value,
            "backup_path": self.backup_path,
            "artifact_sha256": self.artifact_sha256,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.entry_hash != self.compute_hash():
            raise ArtifactApplicationPlanIntegrityError(
                "rollback entry hash does not match content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["entry_hash"] = self.entry_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactRollbackEntry":
        return cls(
            sequence=data["sequence"],
            destination_path=data["destination_path"],
            action=ArtifactRollbackAction(data["action"]),
            backup_path=data.get("backup_path"),
            artifact_sha256=data["artifact_sha256"],
            entry_hash=data.get("entry_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactApplicationPlan:
    application_id: str
    decision: ArtifactApplicationDecision
    request_hash: str
    policy_id: str
    policy_hash: str
    validation_id: str
    validation_result_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    requested_by: str
    approval_reference: str | None
    operations: tuple[ArtifactApplicationOperation, ...]
    rollback_manifest: tuple[ArtifactRollbackEntry, ...]
    reasons: tuple[str, ...]
    generated_at: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    operations_hash: str
    rollback_manifest_hash: str
    plan_hash: str | None = None
    version: int = ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "application_id",
            "policy_id",
            "validation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        try:
            decision = ArtifactApplicationDecision(self.decision)
        except (TypeError, ValueError) as exc:
            raise ArtifactApplicationPlanError(
                "application decision is invalid"
            ) from exc
        object.__setattr__(self, "decision", decision)

        for field_name in (
            "request_hash",
            "policy_hash",
            "validation_result_hash",
            "plan_state_hash",
            "journal_head_hash",
            "journal_hash",
            "operations_hash",
            "rollback_manifest_hash",
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
            "approval_reference",
            _optional_identifier(
                self.approval_reference,
                "approval_reference",
            ),
        )

        operations = tuple(self.operations)
        if not all(
            isinstance(item, ArtifactApplicationOperation)
            for item in operations
        ):
            raise ArtifactApplicationPlanError(
                "operations must contain ArtifactApplicationOperation values"
            )
        if tuple(item.sequence for item in operations) != tuple(
            range(1, len(operations) + 1)
        ):
            raise ArtifactApplicationPlanError(
                "operation sequences must be contiguous from one"
            )
        for item in operations:
            item.verify_hash()
        object.__setattr__(self, "operations", operations)

        manifest = tuple(self.rollback_manifest)
        if not all(
            isinstance(item, ArtifactRollbackEntry)
            for item in manifest
        ):
            raise ArtifactApplicationPlanError(
                "rollback_manifest must contain ArtifactRollbackEntry values"
            )
        if tuple(item.sequence for item in manifest) != tuple(
            range(1, len(manifest) + 1)
        ):
            raise ArtifactApplicationPlanError(
                "rollback sequences must be contiguous from one"
            )
        for item in manifest:
            item.verify_hash()
        object.__setattr__(self, "rollback_manifest", manifest)

        if len(operations) != len(manifest):
            raise ArtifactApplicationPlanIntegrityError(
                "rollback manifest must cover every operation"
            )
        for operation, entry in zip(operations, manifest, strict=True):
            if (
                operation.sequence != entry.sequence
                or operation.destination_path != entry.destination_path
                or operation.rollback_action is not entry.action
                or operation.backup_path != entry.backup_path
                or operation.sha256 != entry.artifact_sha256
            ):
                raise ArtifactApplicationPlanIntegrityError(
                    "rollback manifest does not match operations"
                )

        computed_operations_hash = _sha256_document(
            {
                "record_type": "artifact_application_operations",
                "operations": [
                    item.to_dict()
                    for item in operations
                ],
            }
        )
        if self.operations_hash != computed_operations_hash:
            raise ArtifactApplicationPlanIntegrityError(
                "operations_hash does not match operations"
            )
        computed_manifest_hash = _sha256_document(
            {
                "record_type": "artifact_rollback_manifest",
                "entries": [
                    item.to_dict()
                    for item in manifest
                ],
            }
        )
        if self.rollback_manifest_hash != computed_manifest_hash:
            raise ArtifactApplicationPlanIntegrityError(
                "rollback_manifest_hash does not match manifest"
            )

        reasons = tuple(
            dict.fromkeys(
                _text(item, "reason")
                for item in self.reasons
            )
        )
        object.__setattr__(self, "reasons", reasons)
        if decision is ArtifactApplicationDecision.READY and reasons:
            raise ArtifactApplicationPlanError(
                "ready plan cannot contain blocking reasons"
            )
        if decision is not ArtifactApplicationDecision.READY and not reasons:
            raise ArtifactApplicationPlanError(
                "non-ready plan must contain at least one reason"
            )
        if decision is ArtifactApplicationDecision.REJECTED and operations:
            raise ArtifactApplicationPlanError(
                "rejected plan cannot contain operations"
            )

        object.__setattr__(
            self,
            "generated_at",
            _utc_timestamp(self.generated_at, "generated_at"),
        )
        object.__setattr__(
            self,
            "journal_event_count",
            _non_negative_int(
                self.journal_event_count,
                "journal_event_count",
            ),
        )

        if self.version != ARTIFACT_APPLICATION_PLAN_FORMAT_VERSION:
            raise ArtifactApplicationPlanError(
                "unsupported artifact application plan format version"
            )

        computed = self.compute_hash()
        if self.plan_hash is None:
            object.__setattr__(self, "plan_hash", computed)
        else:
            supplied = _hash(self.plan_hash, "plan_hash")
            if supplied != computed:
                raise ArtifactApplicationPlanIntegrityError(
                    "plan hash does not match plan content"
                )
            object.__setattr__(self, "plan_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_application_plan",
            "version": self.version,
            "application_id": self.application_id,
            "decision": self.decision.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "validation_id": self.validation_id,
            "validation_result_hash": self.validation_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "requested_by": self.requested_by,
            "approval_reference": self.approval_reference,
            "operations": [
                item.to_dict()
                for item in self.operations
            ],
            "rollback_manifest": [
                item.to_dict()
                for item in self.rollback_manifest
            ],
            "reasons": list(self.reasons),
            "generated_at": self.generated_at,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
            "operations_hash": self.operations_hash,
            "rollback_manifest_hash": self.rollback_manifest_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.plan_hash != self.compute_hash():
            raise ArtifactApplicationPlanIntegrityError(
                "plan hash does not match plan content"
            )

    @property
    def executable(self) -> bool:
        return self.decision is ArtifactApplicationDecision.READY

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["plan_hash"] = self.plan_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactApplicationPlan":
        if data.get("record_type") != "artifact_application_plan":
            raise ArtifactApplicationPlanError(
                "record_type must be artifact_application_plan"
            )
        if "plan_hash" not in data:
            raise ArtifactApplicationPlanIntegrityError(
                "serialized plan is missing plan_hash"
            )
        return cls(
            application_id=data["application_id"],
            decision=ArtifactApplicationDecision(data["decision"]),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            validation_id=data["validation_id"],
            validation_result_hash=data["validation_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            requested_by=data["requested_by"],
            approval_reference=data.get("approval_reference"),
            operations=tuple(
                ArtifactApplicationOperation.from_dict(item)
                for item in data["operations"]
            ),
            rollback_manifest=tuple(
                ArtifactRollbackEntry.from_dict(item)
                for item in data["rollback_manifest"]
            ),
            reasons=tuple(data["reasons"]),
            generated_at=data["generated_at"],
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            operations_hash=data["operations_hash"],
            rollback_manifest_hash=data[
                "rollback_manifest_hash"
            ],
            plan_hash=data["plan_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactApplicationPlan":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactApplicationPlanError(
                "artifact application plan JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactApplicationPlanError(
                "artifact application plan JSON must be an object"
            )
        return cls.from_dict(data)


def build_artifact_application_plan(
    request: ArtifactApplicationRequest,
    validation_result: AgentOutputValidationResult,
    policy: ArtifactApplicationPolicy,
) -> ArtifactApplicationPlan:
    """Build a deterministic, non-executing application transaction plan."""

    if not isinstance(request, ArtifactApplicationRequest):
        raise ArtifactApplicationPlanError(
            "request must be an ArtifactApplicationRequest"
        )
    if not isinstance(
        validation_result,
        AgentOutputValidationResult,
    ):
        raise ArtifactApplicationPlanError(
            "validation_result must be an AgentOutputValidationResult"
        )
    if not isinstance(policy, ArtifactApplicationPolicy):
        raise ArtifactApplicationPlanError(
            "policy must be an ArtifactApplicationPolicy"
        )

    request.verify_hash()
    validation_result.verify_hash()
    validation_hash = validation_result.result_hash
    assert validation_hash is not None

    expected = {
        "policy_id": policy.policy_id,
        "policy_hash": policy.policy_hash,
        "validation_id": validation_result.validation_id,
        "validation_result_hash": validation_hash,
        "plan_id": validation_result.plan_id,
        "step_id": validation_result.step_id,
        "agent_id": validation_result.agent_id,
        "plan_state_hash": validation_result.plan_state_hash,
        "journal_event_count": (
            validation_result.journal_event_count
        ),
        "journal_head_hash": (
            validation_result.journal_head_hash
        ),
        "journal_hash": validation_result.journal_hash,
    }
    for field_name, expected_value in expected.items():
        if getattr(request, field_name) != expected_value:
            raise ArtifactApplicationPlanError(
                f"request {field_name} does not match validation result"
            )

    request_hash = request.request_hash
    assert request_hash is not None
    reasons: list[str] = []

    if (
        validation_result.status
        is not AgentOutputValidationStatus.ACCEPTED
    ):
        reasons.append(
            "validation result status must be accepted"
        )

    if len(validation_result.records) > policy.max_operations:
        reasons.append(
            "artifact count exceeds application policy maximum"
        )

    for record in validation_result.records:
        if (
            record.decision
            is not ArtifactValidationDecision.ACCEPTED
        ):
            reasons.append(
                f"artifact index {record.index} is not accepted"
            )
        if (
            record.classification.value
            not in policy.allowed_classifications
        ):
            reasons.append(
                f"artifact index {record.index} classification is not allowed"
            )
        if (
            record.path is None
            or record.sha256 is None
            or record.size_bytes is None
            or record.media_type is None
            or record.operation is None
        ):
            reasons.append(
                f"artifact index {record.index} is incomplete"
            )

    complete_records = tuple(
        record
        for record in validation_result.records
        if (
            record.path is not None
            and record.sha256 is not None
            and record.size_bytes is not None
            and record.media_type is not None
            and record.operation is not None
        )
    )
    path_groups: dict[str, list[ArtifactValidationRecord]] = {}
    for record in complete_records:
        assert record.path is not None
        path_groups.setdefault(
            record.path.casefold(),
            [],
        ).append(record)
    for group in path_groups.values():
        if len(group) > 1:
            reasons.append(
                "duplicate or case-conflicting destination path: "
                + ", ".join(
                    sorted(
                        record.path or ""
                        for record in group
                    )
                )
            )

    if reasons:
        operations: tuple[ArtifactApplicationOperation, ...] = ()
        manifest: tuple[ArtifactRollbackEntry, ...] = ()
        decision = ArtifactApplicationDecision.REJECTED
    else:
        ordered = tuple(
            sorted(
                complete_records,
                key=lambda item: (
                    (item.path or "").casefold(),
                    item.path or "",
                    item.index,
                ),
            )
        )
        built_operations: list[ArtifactApplicationOperation] = []
        built_manifest: list[ArtifactRollbackEntry] = []
        missing_update_approval = False

        for sequence, record in enumerate(ordered, start=1):
            assert record.path is not None
            assert record.sha256 is not None
            assert record.size_bytes is not None
            assert record.media_type is not None
            assert record.operation is not None

            is_update = record.operation is ArtifactOperation.UPDATE
            backup_path = (
                f"{policy.rollback_root}/"
                f"{request_hash[:16]}/"
                f"{record.path}"
                if is_update and policy.rollback_required
                else None
            )
            rollback_action = (
                ArtifactRollbackAction.RESTORE_BACKUP
                if is_update
                else ArtifactRollbackAction.DELETE_CREATED
            )
            approval = (
                request.approval_reference
                if is_update
                else None
            )
            if (
                is_update
                and policy.require_human_approval_for_updates
                and approval is None
            ):
                missing_update_approval = True

            operation = ArtifactApplicationOperation(
                sequence=sequence,
                operation_id=(
                    f"artifact-operation:{request_hash[:48]}:{sequence}"
                ),
                artifact_index=record.index,
                destination_path=record.path,
                operation=record.operation,
                classification=record.classification,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                media_type=record.media_type,
                precondition=(
                    "destination-must-exist-and-be-backed-up"
                    if is_update
                    else "destination-must-not-exist"
                ),
                requires_backup=is_update,
                backup_path=backup_path,
                rollback_action=rollback_action,
                approval_reference=approval,
            )
            built_operations.append(operation)
            built_manifest.append(
                ArtifactRollbackEntry(
                    sequence=sequence,
                    destination_path=record.path,
                    action=rollback_action,
                    backup_path=backup_path,
                    artifact_sha256=record.sha256,
                )
            )

        operations = tuple(built_operations)
        manifest = tuple(built_manifest)
        if missing_update_approval:
            decision = (
                ArtifactApplicationDecision.REQUIRES_APPROVAL
            )
            reasons = (
                ["one or more update operations require human approval"]
            )
        else:
            decision = ArtifactApplicationDecision.READY

    operations_hash = _sha256_document(
        {
            "record_type": "artifact_application_operations",
            "operations": [
                item.to_dict()
                for item in operations
            ],
        }
    )
    manifest_hash = _sha256_document(
        {
            "record_type": "artifact_rollback_manifest",
            "entries": [
                item.to_dict()
                for item in manifest
            ],
        }
    )

    return ArtifactApplicationPlan(
        application_id=request.application_id,
        decision=decision,
        request_hash=request_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        validation_id=validation_result.validation_id,
        validation_result_hash=validation_hash,
        plan_id=validation_result.plan_id,
        step_id=validation_result.step_id,
        agent_id=validation_result.agent_id,
        requested_by=request.requested_by,
        approval_reference=request.approval_reference,
        operations=operations,
        rollback_manifest=manifest,
        reasons=tuple(reasons),
        generated_at=request.requested_at,
        plan_state_hash=validation_result.plan_state_hash,
        journal_event_count=(
            validation_result.journal_event_count
        ),
        journal_head_hash=(
            validation_result.journal_head_hash
        ),
        journal_hash=validation_result.journal_hash,
        operations_hash=operations_hash,
        rollback_manifest_hash=manifest_hash,
    )
