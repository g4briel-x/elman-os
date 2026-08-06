"""Deterministic, read-only selection of persisted orchestration state.

The selector consumes a cryptographically verified
ArtifactOrchestrationStateIndexSnapshot, applies explicit filters, ranks only
valid entries, and either selects one entry or returns a fail-closed no-match
or ambiguity result.

The component never reads or writes the persistence filesystem, never restores
or executes a state, never imports persisted content, never performs network
access, and never invokes an AI provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndexEntry,
    ArtifactOrchestrationStateIndexEntryStatus,
    ArtifactOrchestrationStateIndexSnapshot,
)
from .execution_checkpoint import ResumeAssessmentStatus


ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

_ALL_ASSESSMENT_STATUSES: Final[tuple[ResumeAssessmentStatus, ...]] = tuple(
    sorted(ResumeAssessmentStatus, key=lambda item: item.value)
)


class ArtifactOrchestrationStateSelectionError(RuntimeError):
    """A selection contract or operation is invalid."""


class ArtifactOrchestrationStateSelectionIntegrityError(
    ArtifactOrchestrationStateSelectionError
):
    """A selection contract fails an integrity or cross-binding check."""


class ArtifactOrchestrationStateSelectionLimitError(
    ArtifactOrchestrationStateSelectionError
):
    """A configured selection resource limit is exceeded."""


class ArtifactOrchestrationStateSelectionStrategy(StrEnum):
    LATEST_PERSISTED = "latest-persisted"
    OLDEST_PERSISTED = "oldest-persisted"


class ArtifactOrchestrationStateSelectionRecordDecision(StrEnum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"


class ArtifactOrchestrationStateSelectionStatus(StrEnum):
    SELECTED = "selected"
    NO_MATCH = "no-match"
    AMBIGUOUS = "ambiguous"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationStateSelectionError(
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
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} must be boolean"
        )
    return value


def _optional_boolean(value: object, name: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, name)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} must be a non-negative integer"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationStateSelectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationStateSelectionError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationStateSelectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationStateSelectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationStateSelectionError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationStateSelectionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_utc_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _utc_timestamp(value, name)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_document(data: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json(data).encode("utf-8"))


def _reason_codes(values: object) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ArtifactOrchestrationStateSelectionError(
            "reason_codes must be a tuple or list"
        )
    normalized = tuple(
        _identifier(value, "reason_code", _REASON_CODE)
        for value in values
    )
    if len(set(normalized)) != len(normalized):
        raise ArtifactOrchestrationStateSelectionError(
            "reason_codes contain duplicates"
        )
    if tuple(sorted(normalized)) != normalized:
        raise ArtifactOrchestrationStateSelectionError(
            "reason_codes must be sorted"
        )
    return normalized


def _assessment_statuses(
    values: object,
) -> tuple[ResumeAssessmentStatus, ...]:
    if not isinstance(values, (tuple, list)):
        raise ArtifactOrchestrationStateSelectionError(
            "allowed_assessment_statuses must be a tuple or list"
        )
    try:
        normalized = tuple(
            ResumeAssessmentStatus(value)
            for value in values
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactOrchestrationStateSelectionError(
            "allowed_assessment_statuses contains an invalid status"
        ) from exc
    if len(set(normalized)) != len(normalized):
        raise ArtifactOrchestrationStateSelectionError(
            "allowed_assessment_statuses contains duplicates"
        )
    return tuple(sorted(normalized, key=lambda item: item.value))


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateSelectionPolicy:
    policy_id: str
    strategy: ArtifactOrchestrationStateSelectionStrategy = (
        ArtifactOrchestrationStateSelectionStrategy.LATEST_PERSISTED
    )
    reject_ambiguous: bool = True
    max_snapshot_entries: int = 10_000
    max_eligible_entries: int = 10_000
    version: int = ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        try:
            strategy = ArtifactOrchestrationStateSelectionStrategy(
                self.strategy
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "selection strategy is invalid"
            ) from exc
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(
            self,
            "reject_ambiguous",
            _boolean(self.reject_ambiguous, "reject_ambiguous"),
        )
        object.__setattr__(
            self,
            "max_snapshot_entries",
            _positive_int(
                self.max_snapshot_entries,
                "max_snapshot_entries",
            ),
        )
        object.__setattr__(
            self,
            "max_eligible_entries",
            _positive_int(
                self.max_eligible_entries,
                "max_eligible_entries",
            ),
        )
        if self.max_eligible_entries > self.max_snapshot_entries:
            raise ArtifactOrchestrationStateSelectionError(
                "max_eligible_entries cannot exceed max_snapshot_entries"
            )
        if (
            self.version
            != ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "unsupported state selection format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_selection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "strategy": self.strategy.value,
            "reject_ambiguous": self.reject_ambiguous,
            "max_snapshot_entries": self.max_snapshot_entries,
            "max_eligible_entries": self.max_eligible_entries,
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
    ) -> "ArtifactOrchestrationStateSelectionPolicy":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_selection_policy"
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "record_type must be "
                "artifact_orchestration_state_selection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            strategy=data["strategy"],
            reject_ambiguous=data["reject_ambiguous"],
            max_snapshot_entries=data["max_snapshot_entries"],
            max_eligible_entries=data["max_eligible_entries"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateSelectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "state selection policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateSelectionError(
                "state selection policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateSelectionRequest:
    request_id: str
    snapshot_json: str
    requested_by: str
    requested_at: str
    expected_snapshot_hash: str | None = None
    persistence_id: str | None = None
    project_id: str | None = None
    plan_id: str | None = None
    checkpoint_id: str | None = None
    allowed_assessment_statuses: tuple[ResumeAssessmentStatus, ...] = (
        _ALL_ASSESSMENT_STATUSES
    )
    require_can_resume: bool | None = None
    persisted_not_before: str | None = None
    persisted_not_after: str | None = None
    request_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
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

        snapshot_text = _text(self.snapshot_json, "snapshot_json")
        snapshot = ArtifactOrchestrationStateIndexSnapshot.from_json(
            snapshot_text
        )
        snapshot.verify_hash()
        object.__setattr__(self, "snapshot_json", snapshot.to_json())

        expected_hash = _optional_hash(
            self.expected_snapshot_hash,
            "expected_snapshot_hash",
        )
        if (
            expected_hash is not None
            and expected_hash != snapshot.snapshot_hash
        ):
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "expected_snapshot_hash does not match snapshot"
            )
        object.__setattr__(
            self,
            "expected_snapshot_hash",
            expected_hash,
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
        for field_name in ("project_id", "plan_id", "checkpoint_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )

        object.__setattr__(
            self,
            "allowed_assessment_statuses",
            _assessment_statuses(self.allowed_assessment_statuses),
        )
        object.__setattr__(
            self,
            "require_can_resume",
            _optional_boolean(
                self.require_can_resume,
                "require_can_resume",
            ),
        )
        lower = _optional_utc_timestamp(
            self.persisted_not_before,
            "persisted_not_before",
        )
        upper = _optional_utc_timestamp(
            self.persisted_not_after,
            "persisted_not_after",
        )
        if lower is not None and upper is not None and lower > upper:
            raise ArtifactOrchestrationStateSelectionError(
                "persisted_not_before cannot be after persisted_not_after"
            )
        object.__setattr__(self, "persisted_not_before", lower)
        object.__setattr__(self, "persisted_not_after", upper)

        if (
            self.version
            != ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "unsupported state selection format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def snapshot(self) -> ArtifactOrchestrationStateIndexSnapshot:
        return ArtifactOrchestrationStateIndexSnapshot.from_json(
            self.snapshot_json
        )

    def hash_material(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "record_type": "artifact_orchestration_state_selection_request",
            "version": self.version,
            "request_id": self.request_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "expected_snapshot_hash": self.expected_snapshot_hash,
            "persistence_id": self.persistence_id,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "checkpoint_id": self.checkpoint_id,
            "allowed_assessment_statuses": [
                item.value
                for item in self.allowed_assessment_statuses
            ],
            "require_can_resume": self.require_can_resume,
            "persisted_not_before": self.persisted_not_before,
            "persisted_not_after": self.persisted_not_after,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "request hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["snapshot_json"] = self.snapshot_json
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateSelectionRequest":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_selection_request"
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "record_type must be "
                "artifact_orchestration_state_selection_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "serialized request is missing request_hash"
            )
        if "snapshot_json" not in data:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "serialized request is missing snapshot_json"
            )
        return cls(
            request_id=data["request_id"],
            snapshot_json=data["snapshot_json"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            expected_snapshot_hash=data.get("expected_snapshot_hash"),
            persistence_id=data.get("persistence_id"),
            project_id=data.get("project_id"),
            plan_id=data.get("plan_id"),
            checkpoint_id=data.get("checkpoint_id"),
            allowed_assessment_statuses=tuple(
                data.get(
                    "allowed_assessment_statuses",
                    [
                        item.value
                        for item in _ALL_ASSESSMENT_STATUSES
                    ],
                )
            ),
            require_can_resume=data.get("require_can_resume"),
            persisted_not_before=data.get("persisted_not_before"),
            persisted_not_after=data.get("persisted_not_after"),
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateSelectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "state selection request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateSelectionError(
                "state selection request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateSelectionRecord:
    storage_key: str
    entry_hash: str
    entry_status: ArtifactOrchestrationStateIndexEntryStatus
    decision: ArtifactOrchestrationStateSelectionRecordDecision
    reason_codes: tuple[str, ...]
    primary_rank: str | None = None
    rank_position: int | None = None
    record_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "storage_key",
            _text(self.storage_key, "storage_key"),
        )
        object.__setattr__(
            self,
            "entry_hash",
            _hash(self.entry_hash, "entry_hash"),
        )
        try:
            entry_status = ArtifactOrchestrationStateIndexEntryStatus(
                self.entry_status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "entry_status is invalid"
            ) from exc
        object.__setattr__(self, "entry_status", entry_status)

        try:
            decision = ArtifactOrchestrationStateSelectionRecordDecision(
                self.decision
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "selection record decision is invalid"
            ) from exc
        object.__setattr__(self, "decision", decision)

        reasons = _reason_codes(self.reason_codes)
        object.__setattr__(self, "reason_codes", reasons)

        rank = _optional_utc_timestamp(
            self.primary_rank,
            "primary_rank",
        )
        object.__setattr__(self, "primary_rank", rank)

        if self.rank_position is None:
            position = None
        else:
            position = _positive_int(
                self.rank_position,
                "rank_position",
            )
        object.__setattr__(self, "rank_position", position)

        if decision is ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE:
            if entry_status is not ArtifactOrchestrationStateIndexEntryStatus.VALID:
                raise ArtifactOrchestrationStateSelectionError(
                    "only a valid index entry can be eligible"
                )
            if reasons:
                raise ArtifactOrchestrationStateSelectionError(
                    "eligible record cannot contain exclusion reasons"
                )
            if rank is None or position is None:
                raise ArtifactOrchestrationStateSelectionError(
                    "eligible record requires rank data"
                )
        else:
            if not reasons:
                raise ArtifactOrchestrationStateSelectionError(
                    "excluded record requires at least one reason"
                )
            if rank is not None or position is not None:
                raise ArtifactOrchestrationStateSelectionError(
                    "excluded record cannot contain rank data"
                )

        if (
            self.version
            != ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "unsupported state selection format version"
            )

        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "record hash does not match record content"
                )
            object.__setattr__(self, "record_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_selection_record",
            "version": self.version,
            "storage_key": self.storage_key,
            "entry_hash": self.entry_hash,
            "entry_status": self.entry_status.value,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
            "primary_rank": self.primary_rank,
            "rank_position": self.rank_position,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "record hash does not match record content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationStateSelectionRecord":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_selection_record"
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "record_type must be "
                "artifact_orchestration_state_selection_record"
            )
        if "record_hash" not in data:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "serialized record is missing record_hash"
            )
        return cls(
            storage_key=data["storage_key"],
            entry_hash=data["entry_hash"],
            entry_status=data["entry_status"],
            decision=data["decision"],
            reason_codes=tuple(data["reason_codes"]),
            primary_rank=data.get("primary_rank"),
            rank_position=data.get("rank_position"),
            record_hash=data["record_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateSelectionRecord":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "state selection record JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateSelectionError(
                "state selection record JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateSelectionResult:
    status: ArtifactOrchestrationStateSelectionStatus
    request_id: str
    request_hash: str
    policy_id: str
    policy_hash: str
    snapshot_hash: str
    records: tuple[ArtifactOrchestrationStateSelectionRecord, ...]
    eligible_count: int
    excluded_count: int
    completed_at: str
    reason: str
    selected_entry_json: str | None = None
    selected_record_hash: str | None = None
    result_hash: str | None = None
    version: int = ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        try:
            status = ArtifactOrchestrationStateSelectionStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "selection result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        for field_name in ("request_id", "policy_id"):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "request_hash",
            "policy_hash",
            "snapshot_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )

        records = tuple(self.records)
        if not all(
            isinstance(
                item,
                ArtifactOrchestrationStateSelectionRecord,
            )
            for item in records
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "records must contain selection records"
            )
        for record in records:
            record.verify_hash()
        if len({item.storage_key for item in records}) != len(records):
            raise ArtifactOrchestrationStateSelectionError(
                "records contain duplicate storage keys"
            )
        expected_order = tuple(
            sorted(
                records,
                key=lambda item: (
                    0
                    if item.decision
                    is ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                    else 1,
                    item.rank_position
                    if item.rank_position is not None
                    else 2**31,
                    item.storage_key,
                ),
            )
        )
        if expected_order != records:
            raise ArtifactOrchestrationStateSelectionError(
                "records are not in deterministic result order"
            )
        object.__setattr__(self, "records", records)

        eligible = _non_negative_int(
            self.eligible_count,
            "eligible_count",
        )
        excluded = _non_negative_int(
            self.excluded_count,
            "excluded_count",
        )
        actual_eligible = sum(
            item.decision
            is ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
            for item in records
        )
        actual_excluded = len(records) - actual_eligible
        if eligible != actual_eligible or excluded != actual_excluded:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "selection counters do not match records"
            )
        object.__setattr__(self, "eligible_count", eligible)
        object.__setattr__(self, "excluded_count", excluded)

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

        selected_entry: ArtifactOrchestrationStateIndexEntry | None
        if self.selected_entry_json is None:
            selected_entry = None
            selected_json = None
        else:
            selected_entry = ArtifactOrchestrationStateIndexEntry.from_json(
                _text(self.selected_entry_json, "selected_entry_json")
            )
            selected_entry.verify_hash()
            if (
                selected_entry.status
                is not ArtifactOrchestrationStateIndexEntryStatus.VALID
            ):
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "selected entry must be valid"
                )
            selected_json = selected_entry.to_json()
        object.__setattr__(self, "selected_entry_json", selected_json)

        selected_record_hash = _optional_hash(
            self.selected_record_hash,
            "selected_record_hash",
        )
        object.__setattr__(
            self,
            "selected_record_hash",
            selected_record_hash,
        )

        if status is ArtifactOrchestrationStateSelectionStatus.SELECTED:
            if selected_entry is None or selected_record_hash is None:
                raise ArtifactOrchestrationStateSelectionError(
                    "selected result requires entry and record hash"
                )
            selected_records = [
                record
                for record in records
                if record.record_hash == selected_record_hash
            ]
            if len(selected_records) != 1:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "selected_record_hash does not identify one record"
                )
            selected_record = selected_records[0]
            if (
                selected_record.decision
                is not ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                or selected_record.rank_position != 1
            ):
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "selected record must be the first eligible record"
                )
            if selected_record.storage_key != selected_entry.storage_key:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "selected entry and record storage keys differ"
                )
            if selected_record.entry_hash != selected_entry.entry_hash:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "selected entry and record hashes differ"
                )
        else:
            if selected_entry is not None or selected_record_hash is not None:
                raise ArtifactOrchestrationStateSelectionError(
                    "non-selected result cannot contain a selected entry"
                )
            if (
                status
                is ArtifactOrchestrationStateSelectionStatus.NO_MATCH
                and eligible != 0
            ):
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "no-match result cannot contain eligible records"
                )
            if (
                status
                is ArtifactOrchestrationStateSelectionStatus.AMBIGUOUS
                and eligible < 2
            ):
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "ambiguous result requires at least two eligible records"
                )

        if (
            self.version
            != ARTIFACT_ORCHESTRATION_STATE_SELECTION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "unsupported state selection format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def selected_entry(self) -> ArtifactOrchestrationStateIndexEntry | None:
        if self.selected_entry_json is None:
            return None
        return ArtifactOrchestrationStateIndexEntry.from_json(
            self.selected_entry_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_state_selection_result",
            "version": self.version,
            "status": self.status.value,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "snapshot_hash": self.snapshot_hash,
            "records": [item.to_dict() for item in self.records],
            "eligible_count": self.eligible_count,
            "excluded_count": self.excluded_count,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "selected_entry_json": self.selected_entry_json,
            "selected_record_hash": self.selected_record_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationStateSelectionIntegrityError(
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
    ) -> "ArtifactOrchestrationStateSelectionResult":
        if (
            data.get("record_type")
            != "artifact_orchestration_state_selection_result"
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "record_type must be "
                "artifact_orchestration_state_selection_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "serialized result is missing result_hash"
            )
        raw_records = data.get("records")
        if not isinstance(raw_records, list):
            raise ArtifactOrchestrationStateSelectionError(
                "result records must be a list"
            )
        return cls(
            status=data["status"],
            request_id=data["request_id"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            snapshot_hash=data["snapshot_hash"],
            records=tuple(
                ArtifactOrchestrationStateSelectionRecord.from_dict(item)
                for item in raw_records
            ),
            eligible_count=data["eligible_count"],
            excluded_count=data["excluded_count"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            selected_entry_json=data.get("selected_entry_json"),
            selected_record_hash=data.get("selected_record_hash"),
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationStateSelectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationStateSelectionError(
                "state selection result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationStateSelectionError(
                "state selection result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationStateSelector:
    policy: ArtifactOrchestrationStateSelectionPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.policy,
            ArtifactOrchestrationStateSelectionPolicy,
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "policy must be an ArtifactOrchestrationStateSelectionPolicy"
            )

    def select(
        self,
        request: ArtifactOrchestrationStateSelectionRequest,
    ) -> ArtifactOrchestrationStateSelectionResult:
        if not isinstance(
            request,
            ArtifactOrchestrationStateSelectionRequest,
        ):
            raise ArtifactOrchestrationStateSelectionError(
                "request must be an ArtifactOrchestrationStateSelectionRequest"
            )

        request.verify_hash()
        snapshot = request.snapshot
        snapshot.verify_hash()

        if len(snapshot.entries) > self.policy.max_snapshot_entries:
            raise ArtifactOrchestrationStateSelectionLimitError(
                "snapshot exceeds policy max_snapshot_entries"
            )

        evaluated: list[
            tuple[
                ArtifactOrchestrationStateIndexEntry,
                tuple[str, ...],
            ]
        ] = []
        eligible_entries: list[ArtifactOrchestrationStateIndexEntry] = []

        for entry in snapshot.entries:
            entry.verify_hash()
            reasons = self._exclusion_reasons(entry, request)
            evaluated.append((entry, reasons))
            if not reasons:
                eligible_entries.append(entry)

        if len(eligible_entries) > self.policy.max_eligible_entries:
            raise ArtifactOrchestrationStateSelectionLimitError(
                "eligible entries exceed policy max_eligible_entries"
            )

        ranked = self._rank(eligible_entries)
        positions = {
            entry.storage_key: position
            for position, entry in enumerate(ranked, start=1)
        }

        records_by_key: dict[
            str,
            ArtifactOrchestrationStateSelectionRecord,
        ] = {}
        for entry, reasons in evaluated:
            if reasons:
                record = ArtifactOrchestrationStateSelectionRecord(
                    storage_key=entry.storage_key,
                    entry_hash=entry.entry_hash,
                    entry_status=entry.status,
                    decision=(
                        ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
                    ),
                    reason_codes=reasons,
                )
            else:
                if entry.persisted_at is None:
                    raise ArtifactOrchestrationStateSelectionIntegrityError(
                        "eligible entry is missing persisted_at"
                    )
                record = ArtifactOrchestrationStateSelectionRecord(
                    storage_key=entry.storage_key,
                    entry_hash=entry.entry_hash,
                    entry_status=entry.status,
                    decision=(
                        ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                    ),
                    reason_codes=(),
                    primary_rank=entry.persisted_at,
                    rank_position=positions[entry.storage_key],
                )
            records_by_key[entry.storage_key] = record

        eligible_records = tuple(
            records_by_key[entry.storage_key]
            for entry in ranked
        )
        excluded_records = tuple(
            sorted(
                (
                    record
                    for record in records_by_key.values()
                    if record.decision
                    is ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
                ),
                key=lambda item: item.storage_key,
            )
        )
        records = eligible_records + excluded_records

        if not ranked:
            return self._result(
                status=ArtifactOrchestrationStateSelectionStatus.NO_MATCH,
                request=request,
                snapshot=snapshot,
                records=records,
                reason=(
                    "NO_MATCH: no valid index entry satisfied the explicit "
                    "selection criteria"
                ),
            )

        top_rank = ranked[0].persisted_at
        if top_rank is None:
            raise ArtifactOrchestrationStateSelectionIntegrityError(
                "top-ranked entry is missing persisted_at"
            )
        tied = tuple(
            entry
            for entry in ranked
            if entry.persisted_at == top_rank
        )

        if len(tied) > 1 and self.policy.reject_ambiguous:
            return self._result(
                status=ArtifactOrchestrationStateSelectionStatus.AMBIGUOUS,
                request=request,
                snapshot=snapshot,
                records=records,
                reason=(
                    "AMBIGUOUS: multiple eligible entries share the "
                    "highest primary rank and policy rejects ambiguity"
                ),
            )

        selected = ranked[0]
        selected_record = records_by_key[selected.storage_key]
        return self._result(
            status=ArtifactOrchestrationStateSelectionStatus.SELECTED,
            request=request,
            snapshot=snapshot,
            records=records,
            selected_entry=selected,
            selected_record_hash=selected_record.record_hash,
            reason=(
                "SELECTED: one verified valid orchestration state was "
                "selected by explicit filters and deterministic ranking"
            ),
        )

    def _exclusion_reasons(
        self,
        entry: ArtifactOrchestrationStateIndexEntry,
        request: ArtifactOrchestrationStateSelectionRequest,
    ) -> tuple[str, ...]:
        reasons: set[str] = set()

        if entry.status is not ArtifactOrchestrationStateIndexEntryStatus.VALID:
            reasons.add("entry-not-valid")
            return tuple(sorted(reasons))

        required_values = {
            "persistence-id-mismatch": (
                request.persistence_id,
                entry.persistence_id,
            ),
            "project-id-mismatch": (
                request.project_id,
                entry.project_id,
            ),
            "plan-id-mismatch": (
                request.plan_id,
                entry.plan_id,
            ),
            "checkpoint-id-mismatch": (
                request.checkpoint_id,
                entry.checkpoint_id,
            ),
        }
        for reason, (expected, actual) in required_values.items():
            if expected is not None and expected != actual:
                reasons.add(reason)

        if (
            entry.assessment_status
            not in request.allowed_assessment_statuses
        ):
            reasons.add("assessment-status-not-allowed")

        if (
            request.require_can_resume is not None
            and entry.can_resume != request.require_can_resume
        ):
            reasons.add("can-resume-mismatch")

        if entry.persisted_at is None:
            reasons.add("persisted-at-missing")
        else:
            if (
                request.persisted_not_before is not None
                and entry.persisted_at < request.persisted_not_before
            ):
                reasons.add("persisted-before-lower-bound")
            if (
                request.persisted_not_after is not None
                and entry.persisted_at > request.persisted_not_after
            ):
                reasons.add("persisted-after-upper-bound")

        return tuple(sorted(reasons))

    def _rank(
        self,
        entries: list[ArtifactOrchestrationStateIndexEntry],
    ) -> tuple[ArtifactOrchestrationStateIndexEntry, ...]:
        for entry in entries:
            if entry.persisted_at is None:
                raise ArtifactOrchestrationStateSelectionIntegrityError(
                    "eligible entry is missing persisted_at"
                )

        stable = sorted(entries, key=lambda item: item.storage_key)
        by_time = sorted(
            stable,
            key=lambda item: item.persisted_at,
            reverse=(
                self.policy.strategy
                is ArtifactOrchestrationStateSelectionStrategy.LATEST_PERSISTED
            ),
        )
        return tuple(by_time)

    def _result(
        self,
        *,
        status: ArtifactOrchestrationStateSelectionStatus,
        request: ArtifactOrchestrationStateSelectionRequest,
        snapshot: ArtifactOrchestrationStateIndexSnapshot,
        records: tuple[ArtifactOrchestrationStateSelectionRecord, ...],
        reason: str,
        selected_entry: ArtifactOrchestrationStateIndexEntry | None = None,
        selected_record_hash: str | None = None,
    ) -> ArtifactOrchestrationStateSelectionResult:
        eligible_count = sum(
            item.decision
            is ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
            for item in records
        )
        return ArtifactOrchestrationStateSelectionResult(
            status=status,
            request_id=request.request_id,
            request_hash=request.request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            snapshot_hash=snapshot.snapshot_hash,
            records=records,
            eligible_count=eligible_count,
            excluded_count=len(records) - eligible_count,
            completed_at=request.requested_at,
            reason=reason,
            selected_entry_json=(
                None
                if selected_entry is None
                else selected_entry.to_json()
            ),
            selected_record_hash=selected_record_hash,
        )
