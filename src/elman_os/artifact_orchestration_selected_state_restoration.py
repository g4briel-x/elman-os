"""Controlled restoration of one deterministically selected orchestration state.

This boundary consumes a cryptographically verified
``ArtifactOrchestrationStateSelectionResult`` in ``selected`` status, derives a
read-only restoration request for the exact selected persistence identifier,
restores the persisted plan, journal, and checkpoint through the existing
restoration boundary, and verifies that every restored identity and integrity
field matches the selected index entry.

The component never resumes a plan, executes an agent, imports persisted code,
modifies persisted state, writes to a project workspace, performs network
access, or invokes an AI provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndexEntry,
    ArtifactOrchestrationStateIndexEntryStatus,
)
from .artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationError,
    ArtifactOrchestrationRestorationPolicy,
    ArtifactOrchestrationRestorationRequest,
    ArtifactOrchestrationRestorationResult,
    ArtifactOrchestrationRestoredState,
    ArtifactOrchestrationRestorationStatus,
    ArtifactOrchestrationStateRestoration,
)
from .artifact_orchestration_state_selection import (
    ArtifactOrchestrationStateSelectionResult,
    ArtifactOrchestrationStateSelectionStatus,
)


ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactOrchestrationSelectedStateRestorationError(RuntimeError):
    """A selected-state restoration contract or operation is invalid."""


class ArtifactOrchestrationSelectedStateRestorationSelectionError(
    ArtifactOrchestrationSelectedStateRestorationError
):
    """The supplied selection result cannot authorize restoration."""


class ArtifactOrchestrationSelectedStateRestorationIntegrityError(
    ArtifactOrchestrationSelectedStateRestorationError
):
    """A cryptographic or cross-boundary binding is invalid."""


class ArtifactOrchestrationSelectedStateRestorationExecutionError(
    ArtifactOrchestrationSelectedStateRestorationError
):
    """The delegated read-only restoration operation failed."""


class ArtifactOrchestrationSelectedStateRestorationStatus(StrEnum):
    RESTORED = "restored"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateRestorationError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _payload_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateRestorationError(
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
        raise ArtifactOrchestrationSelectedStateRestorationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateRestorationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _absolute_path(value: object, name: str) -> str:
    try:
        raw_value = os.fspath(value)
    except TypeError as exc:
        raise ArtifactOrchestrationSelectedStateRestorationError(
            f"{name} must be a string or path-like value"
        ) from exc
    raw = _text(raw_value, name)
    path = Path(raw)
    if not path.is_absolute():
        raise ArtifactOrchestrationSelectedStateRestorationError(
            f"{name} must be absolute"
        )
    return path.as_posix()


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationSelectedStateRestorationError(
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
    return _sha256_bytes(canonical_json(data).encode("utf-8"))


def _selected_entry(
    selection: ArtifactOrchestrationStateSelectionResult,
) -> ArtifactOrchestrationStateIndexEntry:
    selection.verify_hash()
    if selection.status is not ArtifactOrchestrationStateSelectionStatus.SELECTED:
        raise ArtifactOrchestrationSelectedStateRestorationSelectionError(
            "selection result must have selected status"
        )
    entry = selection.selected_entry
    if entry is None:
        raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
            "selected result does not embed a selected entry"
        )
    entry.verify_hash()
    if entry.status is not ArtifactOrchestrationStateIndexEntryStatus.VALID:
        raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
            "selected entry must be valid"
        )
    required = {
        "persistence_id": entry.persistence_id,
        "manifest_hash": entry.manifest_hash,
        "orchestration_result_hash": entry.orchestration_result_hash,
        "plan_id": entry.plan_id,
        "project_id": entry.project_id,
        "checkpoint_id": entry.checkpoint_id,
        "assessment_status": entry.assessment_status,
        "can_resume": entry.can_resume,
        "state_hash": entry.state_hash,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
            "selected entry is missing: " + ", ".join(missing)
        )
    return entry


def _verify_entry_location(
    entry: ArtifactOrchestrationStateIndexEntry,
    state_root: str,
) -> None:
    expected = (Path(state_root) / entry.storage_key).as_posix()
    if entry.state_directory != expected:
        raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
            "selected entry state_directory does not match state_root and storage_key"
        )


def _verify_restoration_binding(
    entry: ArtifactOrchestrationStateIndexEntry,
    restoration: ArtifactOrchestrationRestorationResult,
    *,
    state_root: str,
) -> None:
    restoration.verify_hash()
    if restoration.status is not ArtifactOrchestrationRestorationStatus.RESTORED:
        raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
            "delegated restoration did not return restored status"
        )

    expected_directory = (Path(state_root) / entry.storage_key).as_posix()
    direct_bindings = {
        "state_root": (restoration.state_root, state_root),
        "state_directory": (
            restoration.state_directory,
            expected_directory,
        ),
        "persistence_id": (
            restoration.persistence_id,
            entry.persistence_id,
        ),
        "manifest_hash": (
            restoration.manifest_hash,
            entry.manifest_hash,
        ),
        "orchestration_result_hash": (
            restoration.orchestration_result_hash,
            entry.orchestration_result_hash,
        ),
    }
    for name, (actual, expected) in direct_bindings.items():
        if actual != expected:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                f"restoration {name} does not match selected entry"
            )

    restored = restoration.restored_state
    restored.verify_hash()
    state_bindings = {
        "persistence_id": (restored.persistence_id, entry.persistence_id),
        "manifest_hash": (restored.manifest_hash, entry.manifest_hash),
        "orchestration_result_hash": (
            restored.orchestration_result_hash,
            entry.orchestration_result_hash,
        ),
        "plan_id": (restored.plan_id, entry.plan_id),
        "project_id": (restored.project_id, entry.project_id),
        "checkpoint_id": (restored.checkpoint_id, entry.checkpoint_id),
        "assessment_status": (
            restored.assessment_status,
            entry.assessment_status,
        ),
        "can_resume": (restored.can_resume, entry.can_resume),
    }
    for name, (actual, expected) in state_bindings.items():
        if actual != expected:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                f"restored state {name} does not match selected entry"
            )


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateRestorationPolicy:
    policy_id: str
    restoration_policy: ArtifactOrchestrationRestorationPolicy
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        if not isinstance(
            self.restoration_policy,
            ArtifactOrchestrationRestorationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "restoration_policy must be an ArtifactOrchestrationRestorationPolicy"
            )
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "unsupported selected-state restoration format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_restoration_policy"
            ),
            "version": self.version,
            "policy_id": self.policy_id,
            "restoration_policy": self.restoration_policy.to_dict(),
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
    ) -> "ArtifactOrchestrationSelectedStateRestorationPolicy":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_restoration_policy"
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "record_type must be "
                "artifact_orchestration_selected_state_restoration_policy"
            )
        restoration_policy = data.get("restoration_policy")
        if not isinstance(restoration_policy, Mapping):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "restoration_policy must be an object"
            )
        return cls(
            policy_id=data["policy_id"],
            restoration_policy=(
                ArtifactOrchestrationRestorationPolicy.from_dict(
                    restoration_policy
                )
            ),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateRestorationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateRestorationRequest:
    selected_restoration_id: str
    policy_id: str
    policy_hash: str
    selection_result_json: str
    selection_result_hash: str
    state_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        for field_name in ("selected_restoration_id", "policy_id"):
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
        selection_json = _payload_text(
            self.selection_result_json,
            "selection_result_json",
        )
        try:
            selection = ArtifactOrchestrationStateSelectionResult.from_json(
                selection_json
            )
        except Exception as exc:
            raise ArtifactOrchestrationSelectedStateRestorationSelectionError(
                "selection_result_json is invalid"
            ) from exc
        entry = _selected_entry(selection)
        supplied_selection_hash = _hash(
            self.selection_result_hash,
            "selection_result_hash",
        )
        if selection.result_hash != supplied_selection_hash:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "selection_result_hash does not match selection result"
            )
        root = _absolute_path(self.state_root, "state_root")
        _verify_entry_location(entry, root)

        object.__setattr__(
            self,
            "selection_result_json",
            selection.to_json(),
        )
        object.__setattr__(
            self,
            "selection_result_hash",
            supplied_selection_hash,
        )
        object.__setattr__(self, "state_root", root)
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
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "unsupported selected-state restoration format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def selection_result(self) -> ArtifactOrchestrationStateSelectionResult:
        return ArtifactOrchestrationStateSelectionResult.from_json(
            self.selection_result_json
        )

    @property
    def selected_entry(self) -> ArtifactOrchestrationStateIndexEntry:
        return _selected_entry(self.selection_result)

    @classmethod
    def from_selection_result(
        cls,
        selection_result: ArtifactOrchestrationStateSelectionResult,
        policy: ArtifactOrchestrationSelectedStateRestorationPolicy,
        *,
        state_root: str | Path,
        requested_by: str,
        requested_at: str | datetime,
        selected_restoration_id: str | None = None,
    ) -> "ArtifactOrchestrationSelectedStateRestorationRequest":
        if not isinstance(
            selection_result,
            ArtifactOrchestrationStateSelectionResult,
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selection_result must be an "
                "ArtifactOrchestrationStateSelectionResult"
            )
        if not isinstance(
            policy,
            ArtifactOrchestrationSelectedStateRestorationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateRestorationPolicy"
            )
        _selected_entry(selection_result)
        root = _absolute_path(state_root, "state_root")
        result_hash = selection_result.result_hash
        assert result_hash is not None
        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_orchestration_selected_state_restoration_identity"
                ),
                "policy_hash": policy.policy_hash,
                "selection_result_hash": result_hash,
                "state_root": root,
            }
        )
        effective_id = (
            selected_restoration_id
            if selected_restoration_id is not None
            else f"selected-state-restoration:{identity_hash}"
        )
        return cls(
            selected_restoration_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            selection_result_json=selection_result.to_json(),
            selection_result_hash=result_hash,
            state_root=root,
            requested_by=requested_by,
            requested_at=requested_at,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_restoration_request"
            ),
            "version": self.version,
            "selected_restoration_id": self.selected_restoration_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "selection_result_json": self.selection_result_json,
            "selection_result_hash": self.selection_result_hash,
            "state_root": self.state_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateRestorationRequest":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_restoration_request"
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "record_type must be "
                "artifact_orchestration_selected_state_restoration_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            selected_restoration_id=data["selected_restoration_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            selection_result_json=data["selection_result_json"],
            selection_result_hash=data["selection_result_hash"],
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
    ) -> "ArtifactOrchestrationSelectedStateRestorationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateRestorationResult:
    selected_restoration_id: str
    status: ArtifactOrchestrationSelectedStateRestorationStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    selection_result_json: str
    restoration_request_hash: str
    restoration_result_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_restoration_id",
            _identifier(
                self.selected_restoration_id,
                "selected_restoration_id",
            ),
        )
        try:
            status = ArtifactOrchestrationSelectedStateRestorationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        for field_name in (
            "request_hash",
            "policy_hash",
            "restoration_request_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )

        selection_json = _payload_text(
            self.selection_result_json,
            "selection_result_json",
        )
        restoration_json = _payload_text(
            self.restoration_result_json,
            "restoration_result_json",
        )
        try:
            selection = ArtifactOrchestrationStateSelectionResult.from_json(
                selection_json
            )
            restoration = ArtifactOrchestrationRestorationResult.from_json(
                restoration_json
            )
        except Exception as exc:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "result embeds invalid selection or restoration data"
            ) from exc

        entry = _selected_entry(selection)
        _verify_restoration_binding(
            entry,
            restoration,
            state_root=restoration.state_root,
        )
        if restoration.request_hash != self.restoration_request_hash:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "restoration_request_hash does not match restoration result"
            )

        object.__setattr__(
            self,
            "selection_result_json",
            selection.to_json(),
        )
        object.__setattr__(
            self,
            "restoration_result_json",
            restoration.to_json(),
        )
        normalized_completed_at = _utc_timestamp(
            self.completed_at,
            "completed_at",
        )
        if normalized_completed_at != restoration.completed_at:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "completed_at does not match restoration result"
            )
        object.__setattr__(self, "completed_at", normalized_completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESTORATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "unsupported selected-state restoration format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def selection_result(self) -> ArtifactOrchestrationStateSelectionResult:
        return ArtifactOrchestrationStateSelectionResult.from_json(
            self.selection_result_json
        )

    @property
    def selected_entry(self) -> ArtifactOrchestrationStateIndexEntry:
        return _selected_entry(self.selection_result)

    @property
    def restoration_result(self) -> ArtifactOrchestrationRestorationResult:
        return ArtifactOrchestrationRestorationResult.from_json(
            self.restoration_result_json
        )

    @property
    def restored_state(self) -> ArtifactOrchestrationRestoredState:
        return self.restoration_result.restored_state

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_restoration_result"
            ),
            "version": self.version,
            "selected_restoration_id": self.selected_restoration_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "selection_result_json": self.selection_result_json,
            "restoration_request_hash": self.restoration_request_hash,
            "restoration_result_json": self.restoration_result_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateRestorationResult":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_restoration_result"
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "record_type must be "
                "artifact_orchestration_selected_state_restoration_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationSelectedStateRestorationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            selected_restoration_id=data["selected_restoration_id"],
            status=data["status"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            selection_result_json=data["selection_result_json"],
            restoration_request_hash=data["restoration_request_hash"],
            restoration_result_json=data["restoration_result_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateRestorationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "selected-state restoration result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateRestoration:
    request: ArtifactOrchestrationSelectedStateRestorationRequest
    policy: ArtifactOrchestrationSelectedStateRestorationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationSelectedStateRestorationRequest,
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "request must be an "
                "ArtifactOrchestrationSelectedStateRestorationRequest"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationSelectedStateRestorationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateRestorationPolicy"
            )
        self.request.verify_hash()
        if self.request.policy_id != self.policy.policy_id:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "request policy_id does not match policy"
            )
        if self.request.policy_hash != self.policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateRestorationError(
                "request policy_hash does not match policy"
            )

    def restore(
        self,
    ) -> ArtifactOrchestrationSelectedStateRestorationResult:
        self.request.verify_hash()
        selection = self.request.selection_result
        entry = _selected_entry(selection)
        _verify_entry_location(entry, self.request.state_root)

        persistence_id = entry.persistence_id
        manifest_hash = entry.manifest_hash
        orchestration_result_hash = entry.orchestration_result_hash
        assert persistence_id is not None
        assert manifest_hash is not None
        assert orchestration_result_hash is not None

        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_orchestration_selected_state_delegated_identity"
                ),
                "selected_restoration_id": (
                    self.request.selected_restoration_id
                ),
                "selection_result_hash": (
                    self.request.selection_result_hash
                ),
                "restoration_policy_hash": (
                    self.policy.restoration_policy.policy_hash
                ),
                "persistence_id": persistence_id,
                "state_root": self.request.state_root,
            }
        )
        restoration_request = (
            ArtifactOrchestrationRestorationRequest.from_identifiers(
                persistence_id=persistence_id,
                state_root=self.request.state_root,
                policy=self.policy.restoration_policy,
                requested_by=self.request.requested_by,
                requested_at=self.request.requested_at,
                expected_manifest_hash=manifest_hash,
                expected_orchestration_result_hash=(
                    orchestration_result_hash
                ),
                restoration_id=f"selected-restoration:{identity_hash}",
            )
        )

        try:
            restoration_result = ArtifactOrchestrationStateRestoration(
                restoration_request,
                self.policy.restoration_policy,
            ).restore()
        except ArtifactOrchestrationRestorationError as exc:
            raise ArtifactOrchestrationSelectedStateRestorationExecutionError(
                "delegated selected-state restoration failed"
            ) from exc

        _verify_restoration_binding(
            entry,
            restoration_result,
            state_root=self.request.state_root,
        )

        request_hash = self.request.request_hash
        restoration_request_hash = restoration_request.request_hash
        assert request_hash is not None
        assert restoration_request_hash is not None
        return ArtifactOrchestrationSelectedStateRestorationResult(
            selected_restoration_id=(
                self.request.selected_restoration_id
            ),
            status=(
                ArtifactOrchestrationSelectedStateRestorationStatus.RESTORED
            ),
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            selection_result_json=selection.to_json(),
            restoration_request_hash=restoration_request_hash,
            restoration_result_json=restoration_result.to_json(),
            completed_at=restoration_result.completed_at,
            reason=(
                "RESTORED: the selected persisted orchestration state was "
                "restored read-only and matched every selected index binding"
            ),
        )
