"""Declarative validation of agent-produced artifact outputs for ELMAN-OS v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from .agent_contracts import (
    AgentResponseStatus,
    canonical_json,
)
from .agent_response_ingestion import (
    AgentResponseIngestionResult,
)


AGENT_OUTPUT_VALIDATION_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

_ALLOWED_ARTIFACT_KEYS = {
    "path",
    "sha256",
    "size_bytes",
    "media_type",
    "kind",
    "operation",
    "executable",
    "metadata",
}


class AgentOutputValidationError(ValueError):
    """An output validation request, policy, record, or result is malformed."""


class AgentOutputValidationIntegrityError(AgentOutputValidationError):
    """A validation artifact fails a cryptographic or structural check."""


class ArtifactClassification(StrEnum):
    SOURCE = "source"
    TEST = "test"
    DOCUMENTATION = "documentation"
    CONFIGURATION = "configuration"
    DATA = "data"
    REPORT = "report"
    PATCH = "patch"
    ARCHIVE = "archive"
    BINARY = "binary"
    OTHER = "other"


class ArtifactOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class ArtifactValidationDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


class AgentOutputValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentOutputValidationError(
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
        raise AgentOutputValidationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise AgentOutputValidationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise AgentOutputValidationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise AgentOutputValidationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise AgentOutputValidationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise AgentOutputValidationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise AgentOutputValidationError(
                f"{name} must be UTC"
            )
    else:
        raise AgentOutputValidationError(
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
        raise AgentOutputValidationError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise AgentOutputValidationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _strings(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentOutputValidationError(
            f"{name} must be an iterable"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise AgentOutputValidationError(
            f"{name} must be an iterable"
        ) from exc
    return tuple(
        sorted(
            {
                _text(item, name)
                for item in items
            }
        )
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _plan_hash(result: AgentResponseIngestionResult) -> str:
    return hashlib.sha256(
        result.updated_plan.to_json().encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentOutputValidationPolicy:
    policy_id: str
    max_artifacts: int = 64
    max_artifact_bytes: int = 10_000_000
    max_total_bytes: int = 50_000_000
    review_kinds: tuple[str, ...] = (
        ArtifactClassification.ARCHIVE.value,
        ArtifactClassification.BINARY.value,
        ArtifactClassification.OTHER.value,
        ArtifactClassification.PATCH.value,
    )
    review_extensions: tuple[str, ...] = (
        ".7z",
        ".bat",
        ".cmd",
        ".diff",
        ".gz",
        ".jar",
        ".patch",
        ".ps1",
        ".rar",
        ".sh",
        ".tar",
        ".whl",
        ".zip",
    )
    forbidden_extensions: tuple[str, ...] = (
        ".com",
        ".dll",
        ".dylib",
        ".exe",
        ".msi",
        ".pyd",
        ".scr",
        ".so",
    )
    forbidden_names: tuple[str, ...] = (
        ".env",
        ".npmrc",
        ".pypirc",
        "id_ed25519",
        "id_rsa",
    )
    forbidden_suffixes: tuple[str, ...] = (
        ".key",
        ".p12",
        ".pem",
        ".pfx",
    )
    review_path_prefixes: tuple[str, ...] = (
        ".github/workflows/",
        "infrastructure/",
        "scripts/release/",
        "security/",
    )
    max_path_length: int = 240
    max_segment_length: int = 100
    version: int = AGENT_OUTPUT_VALIDATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "max_artifacts",
            "max_artifact_bytes",
            "max_total_bytes",
            "max_path_length",
            "max_segment_length",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )

        if self.max_total_bytes < self.max_artifact_bytes:
            raise AgentOutputValidationError(
                "max_total_bytes cannot be smaller than max_artifact_bytes"
            )

        review_kinds = _strings(
            self.review_kinds,
            "review_kinds",
        )
        valid_kinds = {
            item.value
            for item in ArtifactClassification
        }
        if not set(review_kinds).issubset(valid_kinds):
            raise AgentOutputValidationError(
                "review_kinds contains an unknown artifact classification"
            )
        object.__setattr__(self, "review_kinds", review_kinds)

        for field_name in (
            "review_extensions",
            "forbidden_extensions",
            "forbidden_names",
            "forbidden_suffixes",
            "review_path_prefixes",
        ):
            normalized = tuple(
                sorted(
                    {
                        _text(item, field_name).lower()
                        for item in getattr(self, field_name)
                    }
                )
            )
            object.__setattr__(self, field_name, normalized)

        if (
            set(self.review_extensions)
            & set(self.forbidden_extensions)
        ):
            raise AgentOutputValidationError(
                "review and forbidden extensions overlap"
            )

        if (
            self.version
            != AGENT_OUTPUT_VALIDATION_FORMAT_VERSION
        ):
            raise AgentOutputValidationError(
                "unsupported output validation format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "agent_output_validation_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "max_artifacts": self.max_artifacts,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_total_bytes": self.max_total_bytes,
            "review_kinds": list(self.review_kinds),
            "review_extensions": list(self.review_extensions),
            "forbidden_extensions": list(
                self.forbidden_extensions
            ),
            "forbidden_names": list(self.forbidden_names),
            "forbidden_suffixes": list(
                self.forbidden_suffixes
            ),
            "review_path_prefixes": list(
                self.review_path_prefixes
            ),
            "max_path_length": self.max_path_length,
            "max_segment_length": self.max_segment_length,
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
    ) -> "AgentOutputValidationPolicy":
        if (
            data.get("record_type")
            != "agent_output_validation_policy"
        ):
            raise AgentOutputValidationError(
                "record_type must be agent_output_validation_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            max_artifacts=data["max_artifacts"],
            max_artifact_bytes=data["max_artifact_bytes"],
            max_total_bytes=data["max_total_bytes"],
            review_kinds=tuple(data["review_kinds"]),
            review_extensions=tuple(
                data["review_extensions"]
            ),
            forbidden_extensions=tuple(
                data["forbidden_extensions"]
            ),
            forbidden_names=tuple(data["forbidden_names"]),
            forbidden_suffixes=tuple(
                data["forbidden_suffixes"]
            ),
            review_path_prefixes=tuple(
                data["review_path_prefixes"]
            ),
            max_path_length=data["max_path_length"],
            max_segment_length=data["max_segment_length"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "AgentOutputValidationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentOutputValidationError(
                "output validation policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise AgentOutputValidationError(
                "output validation policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class AgentOutputValidationRequest:
    validation_id: str
    policy_id: str
    policy_hash: str
    ingestion_id: str
    ingestion_result_hash: str
    plan_id: str
    step_id: str
    agent_request_id: str
    agent_id: str
    response_status: AgentResponseStatus
    response_hash: str
    requested_at: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    request_hash: str | None = None
    version: int = AGENT_OUTPUT_VALIDATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "validation_id",
            "policy_id",
            "ingestion_id",
            "agent_request_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        object.__setattr__(
            self,
            "ingestion_result_hash",
            _hash(
                self.ingestion_result_hash,
                "ingestion_result_hash",
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
        try:
            response_status = AgentResponseStatus(
                self.response_status
            )
        except (TypeError, ValueError) as exc:
            raise AgentOutputValidationError(
                "response_status is invalid"
            ) from exc
        object.__setattr__(
            self,
            "response_status",
            response_status,
        )
        object.__setattr__(
            self,
            "response_hash",
            _hash(self.response_hash, "response_hash"),
        )
        object.__setattr__(
            self,
            "requested_at",
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "plan_state_hash",
            _hash(self.plan_state_hash, "plan_state_hash"),
        )
        object.__setattr__(
            self,
            "journal_event_count",
            _non_negative_int(
                self.journal_event_count,
                "journal_event_count",
            ),
        )
        object.__setattr__(
            self,
            "journal_head_hash",
            _hash(self.journal_head_hash, "journal_head_hash"),
        )
        object.__setattr__(
            self,
            "journal_hash",
            _hash(self.journal_hash, "journal_hash"),
        )

        if (
            self.version
            != AGENT_OUTPUT_VALIDATION_FORMAT_VERSION
        ):
            raise AgentOutputValidationError(
                "unsupported output validation format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise AgentOutputValidationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_ingestion_result(
        cls,
        result: AgentResponseIngestionResult,
        policy: AgentOutputValidationPolicy,
        *,
        requested_at: str | datetime,
        validation_id: str | None = None,
    ) -> "AgentOutputValidationRequest":
        if not isinstance(result, AgentResponseIngestionResult):
            raise AgentOutputValidationError(
                "result must be an AgentResponseIngestionResult"
            )
        if not isinstance(policy, AgentOutputValidationPolicy):
            raise AgentOutputValidationError(
                "policy must be an AgentOutputValidationPolicy"
            )

        result.verify_hash()
        result_hash = result.result_hash
        assert result_hash is not None
        seal = result.to_journal().seal()
        normalized_time = _utc_timestamp(
            requested_at,
            "requested_at",
        )

        source_hash = _sha256_document(
            {
                "record_type": (
                    "agent_output_validation_request_source"
                ),
                "policy_hash": policy.policy_hash,
                "ingestion_id": result.ingestion_id,
                "ingestion_result_hash": result_hash,
                "plan_id": result.plan_id,
                "step_id": result.step_id,
                "agent_request_id": result.agent_request_id,
                "agent_id": result.agent_id,
                "response_status": result.response.status.value,
                "response_hash": result.response_hash,
                "requested_at": normalized_time,
                "plan_state_hash": _plan_hash(result),
                "journal_event_count": seal.event_count,
                "journal_head_hash": seal.head_hash,
                "journal_hash": seal.journal_hash,
            }
        )

        effective_validation_id = (
            validation_id
            if validation_id is not None
            else f"output-validation:{source_hash}"
        )

        return cls(
            validation_id=effective_validation_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            ingestion_id=result.ingestion_id,
            ingestion_result_hash=result_hash,
            plan_id=result.plan_id,
            step_id=result.step_id,
            agent_request_id=result.agent_request_id,
            agent_id=result.agent_id,
            response_status=result.response.status,
            response_hash=result.response_hash,
            requested_at=normalized_time,
            plan_state_hash=_plan_hash(result),
            journal_event_count=seal.event_count,
            journal_head_hash=seal.head_hash,
            journal_hash=seal.journal_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "agent_output_validation_request",
            "version": self.version,
            "validation_id": self.validation_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "ingestion_id": self.ingestion_id,
            "ingestion_result_hash": self.ingestion_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_request_id": self.agent_request_id,
            "agent_id": self.agent_id,
            "response_status": self.response_status.value,
            "response_hash": self.response_hash,
            "requested_at": self.requested_at,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise AgentOutputValidationIntegrityError(
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
    ) -> "AgentOutputValidationRequest":
        if (
            data.get("record_type")
            != "agent_output_validation_request"
        ):
            raise AgentOutputValidationError(
                "record_type must be agent_output_validation_request"
            )
        if "request_hash" not in data:
            raise AgentOutputValidationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            validation_id=data["validation_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            ingestion_id=data["ingestion_id"],
            ingestion_result_hash=data["ingestion_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_request_id=data["agent_request_id"],
            agent_id=data["agent_id"],
            response_status=AgentResponseStatus(
                data["response_status"]
            ),
            response_hash=data["response_hash"],
            requested_at=data["requested_at"],
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
    ) -> "AgentOutputValidationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentOutputValidationError(
                "output validation request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise AgentOutputValidationError(
                "output validation request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactValidationRecord:
    index: int
    path: str | None
    decision: ArtifactValidationDecision
    classification: ArtifactClassification
    operation: ArtifactOperation | None
    sha256: str | None
    size_bytes: int | None
    media_type: str | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        if self.path is not None:
            object.__setattr__(
                self,
                "path",
                _text(self.path, "path"),
            )
        try:
            decision = ArtifactValidationDecision(
                self.decision
            )
        except (TypeError, ValueError) as exc:
            raise AgentOutputValidationError(
                "artifact validation decision is invalid"
            ) from exc
        object.__setattr__(self, "decision", decision)
        try:
            classification = ArtifactClassification(
                self.classification
            )
        except (TypeError, ValueError) as exc:
            raise AgentOutputValidationError(
                "artifact classification is invalid"
            ) from exc
        object.__setattr__(
            self,
            "classification",
            classification,
        )

        if self.operation is not None:
            try:
                operation = ArtifactOperation(self.operation)
            except (TypeError, ValueError) as exc:
                raise AgentOutputValidationError(
                    "artifact operation is invalid"
                ) from exc
            object.__setattr__(self, "operation", operation)

        if self.sha256 is not None:
            object.__setattr__(
                self,
                "sha256",
                _hash(self.sha256, "sha256"),
            )
        if self.size_bytes is not None:
            object.__setattr__(
                self,
                "size_bytes",
                _non_negative_int(
                    self.size_bytes,
                    "size_bytes",
                ),
            )
        if self.media_type is not None:
            media_type = _text(
                self.media_type,
                "media_type",
            )
            if _MEDIA_TYPE.fullmatch(media_type) is None:
                raise AgentOutputValidationError(
                    "media_type is invalid"
                )
            object.__setattr__(
                self,
                "media_type",
                media_type,
            )

        reasons = tuple(
            dict.fromkeys(
                _text(item, "reason")
                for item in self.reasons
            )
        )
        if not reasons:
            raise AgentOutputValidationError(
                "artifact validation record must contain a reason"
            )
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "decision": self.decision.value,
            "classification": self.classification.value,
            "operation": (
                self.operation.value
                if self.operation is not None
                else None
            ),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactValidationRecord":
        operation = data.get("operation")
        return cls(
            index=data["index"],
            path=data.get("path"),
            decision=ArtifactValidationDecision(
                data["decision"]
            ),
            classification=ArtifactClassification(
                data["classification"]
            ),
            operation=(
                ArtifactOperation(operation)
                if operation is not None
                else None
            ),
            sha256=data.get("sha256"),
            size_bytes=data.get("size_bytes"),
            media_type=data.get("media_type"),
            reasons=tuple(data["reasons"]),
        )


@dataclass(frozen=True, slots=True)
class AgentOutputValidationResult:
    validation_id: str
    status: AgentOutputValidationStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    ingestion_id: str
    ingestion_result_hash: str
    plan_id: str
    step_id: str
    agent_request_id: str
    agent_id: str
    response_hash: str
    records: tuple[ArtifactValidationRecord, ...]
    top_level_reasons: tuple[str, ...]
    accepted_count: int
    review_count: int
    rejected_count: int
    total_declared_bytes: int
    validated_at: str
    plan_state_hash: str
    journal_event_count: int
    journal_head_hash: str
    journal_hash: str
    result_hash: str | None = None
    version: int = AGENT_OUTPUT_VALIDATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "validation_id",
            "policy_id",
            "ingestion_id",
            "agent_request_id",
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
            status = AgentOutputValidationStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise AgentOutputValidationError(
                "output validation status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        for field_name in (
            "request_hash",
            "policy_hash",
            "ingestion_result_hash",
            "response_hash",
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

        records = tuple(self.records)
        if not all(
            isinstance(item, ArtifactValidationRecord)
            for item in records
        ):
            raise AgentOutputValidationError(
                "records must contain ArtifactValidationRecord values"
            )
        if tuple(item.index for item in records) != tuple(
            range(len(records))
        ):
            raise AgentOutputValidationError(
                "artifact record indexes must be contiguous from zero"
            )
        object.__setattr__(self, "records", records)

        top_level_reasons = tuple(
            dict.fromkeys(
                _text(item, "top_level_reason")
                for item in self.top_level_reasons
            )
        )
        object.__setattr__(
            self,
            "top_level_reasons",
            top_level_reasons,
        )

        for field_name in (
            "accepted_count",
            "review_count",
            "rejected_count",
            "total_declared_bytes",
            "journal_event_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )

        actual_accepted = sum(
            item.decision
            is ArtifactValidationDecision.ACCEPTED
            for item in records
        )
        actual_review = sum(
            item.decision
            is ArtifactValidationDecision.REQUIRES_REVIEW
            for item in records
        )
        actual_rejected = sum(
            item.decision
            is ArtifactValidationDecision.REJECTED
            for item in records
        )
        if (
            self.accepted_count,
            self.review_count,
            self.rejected_count,
        ) != (
            actual_accepted,
            actual_review,
            actual_rejected,
        ):
            raise AgentOutputValidationIntegrityError(
                "artifact decision counts do not match records"
            )

        expected_total = sum(
            item.size_bytes
            for item in records
            if item.size_bytes is not None
        )
        if self.total_declared_bytes != expected_total:
            raise AgentOutputValidationIntegrityError(
                "total_declared_bytes does not match records"
            )

        expected_status = (
            AgentOutputValidationStatus.REJECTED
            if actual_rejected
            or any(
                reason.startswith("REJECTED:")
                for reason in top_level_reasons
            )
            else AgentOutputValidationStatus.REQUIRES_REVIEW
            if actual_review
            or any(
                reason.startswith("REVIEW:")
                for reason in top_level_reasons
            )
            else AgentOutputValidationStatus.ACCEPTED
        )
        if status is not expected_status:
            raise AgentOutputValidationIntegrityError(
                "validation status does not match records and reasons"
            )

        object.__setattr__(
            self,
            "validated_at",
            _utc_timestamp(self.validated_at, "validated_at"),
        )

        if (
            self.version
            != AGENT_OUTPUT_VALIDATION_FORMAT_VERSION
        ):
            raise AgentOutputValidationError(
                "unsupported output validation format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise AgentOutputValidationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "agent_output_validation_result",
            "version": self.version,
            "validation_id": self.validation_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "ingestion_id": self.ingestion_id,
            "ingestion_result_hash": self.ingestion_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_request_id": self.agent_request_id,
            "agent_id": self.agent_id,
            "response_hash": self.response_hash,
            "records": [
                item.to_dict()
                for item in self.records
            ],
            "top_level_reasons": list(
                self.top_level_reasons
            ),
            "accepted_count": self.accepted_count,
            "review_count": self.review_count,
            "rejected_count": self.rejected_count,
            "total_declared_bytes": self.total_declared_bytes,
            "validated_at": self.validated_at,
            "plan_state_hash": self.plan_state_hash,
            "journal_event_count": self.journal_event_count,
            "journal_head_hash": self.journal_head_hash,
            "journal_hash": self.journal_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise AgentOutputValidationIntegrityError(
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
    ) -> "AgentOutputValidationResult":
        if (
            data.get("record_type")
            != "agent_output_validation_result"
        ):
            raise AgentOutputValidationError(
                "record_type must be agent_output_validation_result"
            )
        if "result_hash" not in data:
            raise AgentOutputValidationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            validation_id=data["validation_id"],
            status=AgentOutputValidationStatus(data["status"]),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            ingestion_id=data["ingestion_id"],
            ingestion_result_hash=data["ingestion_result_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_request_id=data["agent_request_id"],
            agent_id=data["agent_id"],
            response_hash=data["response_hash"],
            records=tuple(
                ArtifactValidationRecord.from_dict(item)
                for item in data["records"]
            ),
            top_level_reasons=tuple(
                data["top_level_reasons"]
            ),
            accepted_count=data["accepted_count"],
            review_count=data["review_count"],
            rejected_count=data["rejected_count"],
            total_declared_bytes=data["total_declared_bytes"],
            validated_at=data["validated_at"],
            plan_state_hash=data["plan_state_hash"],
            journal_event_count=data["journal_event_count"],
            journal_head_hash=data["journal_head_hash"],
            journal_hash=data["journal_hash"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "AgentOutputValidationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentOutputValidationError(
                "output validation result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise AgentOutputValidationError(
                "output validation result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(slots=True)
class _Candidate:
    index: int
    path: str | None = None
    classification: ArtifactClassification = (
        ArtifactClassification.OTHER
    )
    operation: ArtifactOperation | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    media_type: str | None = None
    rejected: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)

    def record(self) -> ArtifactValidationRecord:
        if self.rejected:
            decision = ArtifactValidationDecision.REJECTED
            reasons = tuple(
                f"REJECTED: {reason}"
                for reason in dict.fromkeys(self.rejected)
            )
        elif self.review:
            decision = (
                ArtifactValidationDecision.REQUIRES_REVIEW
            )
            reasons = tuple(
                f"REVIEW: {reason}"
                for reason in dict.fromkeys(self.review)
            )
        else:
            decision = ArtifactValidationDecision.ACCEPTED
            reasons = (
                "ACCEPTED: artifact declaration satisfies policy",
            )

        return ArtifactValidationRecord(
            index=self.index,
            path=self.path,
            decision=decision,
            classification=self.classification,
            operation=self.operation,
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            media_type=self.media_type,
            reasons=reasons,
        )


@dataclass(frozen=True, slots=True)
class AgentOutputValidation:
    request: AgentOutputValidationRequest
    ingestion_result: AgentResponseIngestionResult
    policy: AgentOutputValidationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            AgentOutputValidationRequest,
        ):
            raise AgentOutputValidationError(
                "request must be an AgentOutputValidationRequest"
            )
        if not isinstance(
            self.ingestion_result,
            AgentResponseIngestionResult,
        ):
            raise AgentOutputValidationError(
                "ingestion_result must be an AgentResponseIngestionResult"
            )
        if not isinstance(
            self.policy,
            AgentOutputValidationPolicy,
        ):
            raise AgentOutputValidationError(
                "policy must be an AgentOutputValidationPolicy"
            )

        self.request.verify_hash()
        self.ingestion_result.verify_hash()

        result_hash = self.ingestion_result.result_hash
        assert result_hash is not None
        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "ingestion_id": self.ingestion_result.ingestion_id,
            "ingestion_result_hash": result_hash,
            "plan_id": self.ingestion_result.plan_id,
            "step_id": self.ingestion_result.step_id,
            "agent_request_id": (
                self.ingestion_result.agent_request_id
            ),
            "agent_id": self.ingestion_result.agent_id,
            "response_status": (
                self.ingestion_result.response.status
            ),
            "response_hash": (
                self.ingestion_result.response_hash
            ),
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise AgentOutputValidationError(
                    f"request {field_name} does not match source result"
                )

        seal = self.ingestion_result.to_journal().seal()
        boundary = {
            "plan_state_hash": _plan_hash(
                self.ingestion_result
            ),
            "journal_event_count": seal.event_count,
            "journal_head_hash": seal.head_hash,
            "journal_hash": seal.journal_hash,
        }
        for field_name, expected_value in boundary.items():
            if getattr(self.request, field_name) != expected_value:
                raise AgentOutputValidationError(
                    f"request {field_name} does not match source boundary"
                )

    def validate(self) -> AgentOutputValidationResult:
        self.request.verify_hash()
        self.ingestion_result.verify_hash()

        top_rejected: list[str] = []
        top_review: list[str] = []
        candidates: list[_Candidate] = []

        response = self.ingestion_result.response
        outputs = response.outputs

        if response.status is not AgentResponseStatus.SUCCEEDED:
            top_rejected.append(
                "agent response status is not succeeded"
            )

        unknown_top_keys = tuple(
            sorted(
                set(outputs)
                - {"artifact", "artifacts"}
            )
        )
        if unknown_top_keys:
            top_review.append(
                "unrecognized top-level output keys: "
                + ", ".join(unknown_top_keys)
            )

        has_single = "artifact" in outputs
        has_multiple = "artifacts" in outputs
        raw_artifacts: tuple[object, ...]

        if has_single and has_multiple:
            top_rejected.append(
                "outputs cannot contain both artifact and artifacts"
            )
            raw_artifacts = ()
        elif has_single:
            raw_artifacts = (outputs["artifact"],)
        elif has_multiple:
            raw_value = outputs["artifacts"]
            if isinstance(raw_value, (str, bytes, Mapping)):
                top_rejected.append(
                    "artifacts must be an array of objects"
                )
                raw_artifacts = ()
            elif isinstance(raw_value, Sequence):
                raw_artifacts = tuple(raw_value)
            else:
                top_rejected.append(
                    "artifacts must be an array of objects"
                )
                raw_artifacts = ()
        else:
            top_review.append(
                "response declares no artifact or artifacts collection"
            )
            raw_artifacts = ()

        if len(raw_artifacts) > self.policy.max_artifacts:
            top_rejected.append(
                "artifact count exceeds policy maximum"
            )
            raw_artifacts = raw_artifacts[
                : self.policy.max_artifacts
            ]

        for index, raw in enumerate(raw_artifacts):
            candidates.append(
                self._validate_artifact(index, raw)
            )

        self._detect_path_conflicts(candidates)
        self._detect_content_duplicates(candidates)

        total_declared_bytes = sum(
            item.size_bytes
            for item in candidates
            if item.size_bytes is not None
        )
        if total_declared_bytes > self.policy.max_total_bytes:
            top_rejected.append(
                "total declared artifact size exceeds policy maximum"
            )

        records = tuple(
            candidate.record()
            for candidate in candidates
        )
        rejected_count = sum(
            item.decision
            is ArtifactValidationDecision.REJECTED
            for item in records
        )
        review_count = sum(
            item.decision
            is ArtifactValidationDecision.REQUIRES_REVIEW
            for item in records
        )
        accepted_count = sum(
            item.decision
            is ArtifactValidationDecision.ACCEPTED
            for item in records
        )

        top_level_reasons = tuple(
            [
                *(
                    f"REJECTED: {reason}"
                    for reason in dict.fromkeys(top_rejected)
                ),
                *(
                    f"REVIEW: {reason}"
                    for reason in dict.fromkeys(top_review)
                ),
            ]
        )

        status = (
            AgentOutputValidationStatus.REJECTED
            if rejected_count or top_rejected
            else AgentOutputValidationStatus.REQUIRES_REVIEW
            if review_count or top_review
            else AgentOutputValidationStatus.ACCEPTED
        )

        request_hash = self.request.request_hash
        assert request_hash is not None
        ingestion_result_hash = (
            self.ingestion_result.result_hash
        )
        assert ingestion_result_hash is not None

        return AgentOutputValidationResult(
            validation_id=self.request.validation_id,
            status=status,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            ingestion_id=self.ingestion_result.ingestion_id,
            ingestion_result_hash=ingestion_result_hash,
            plan_id=self.ingestion_result.plan_id,
            step_id=self.ingestion_result.step_id,
            agent_request_id=(
                self.ingestion_result.agent_request_id
            ),
            agent_id=self.ingestion_result.agent_id,
            response_hash=self.ingestion_result.response_hash,
            records=records,
            top_level_reasons=top_level_reasons,
            accepted_count=accepted_count,
            review_count=review_count,
            rejected_count=rejected_count,
            total_declared_bytes=total_declared_bytes,
            validated_at=self.request.requested_at,
            plan_state_hash=self.request.plan_state_hash,
            journal_event_count=(
                self.request.journal_event_count
            ),
            journal_head_hash=self.request.journal_head_hash,
            journal_hash=self.request.journal_hash,
        )

    def _validate_artifact(
        self,
        index: int,
        raw: object,
    ) -> _Candidate:
        candidate = _Candidate(index=index)

        if not isinstance(raw, Mapping):
            candidate.rejected.append(
                "artifact declaration must be an object"
            )
            return candidate

        unknown_keys = tuple(
            sorted(
                set(raw)
                - _ALLOWED_ARTIFACT_KEYS
            )
        )
        if unknown_keys:
            candidate.rejected.append(
                "unknown artifact fields: "
                + ", ".join(unknown_keys)
            )

        required = {
            "path",
            "sha256",
            "size_bytes",
            "media_type",
            "kind",
        }
        missing = tuple(
            sorted(required - set(raw))
        )
        if missing:
            candidate.rejected.append(
                "missing artifact fields: "
                + ", ".join(missing)
            )

        candidate.path = self._validate_path(
            raw.get("path"),
            candidate,
        )
        candidate.sha256 = self._validate_sha256(
            raw.get("sha256"),
            candidate,
        )
        candidate.size_bytes = self._validate_size(
            raw.get("size_bytes"),
            candidate,
        )
        candidate.media_type = self._validate_media_type(
            raw.get("media_type"),
            candidate,
        )
        candidate.classification = self._validate_kind(
            raw.get("kind"),
            candidate,
        )
        candidate.operation = self._validate_operation(
            raw.get("operation", ArtifactOperation.CREATE.value),
            candidate,
        )

        executable = raw.get("executable", False)
        if not isinstance(executable, bool):
            candidate.rejected.append(
                "executable must be boolean"
            )
        elif executable:
            candidate.review.append(
                "artifact is declared executable"
            )

        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            candidate.rejected.append(
                "metadata must be an object"
            )

        if candidate.operation is ArtifactOperation.UPDATE:
            candidate.review.append(
                "update operation requires workspace review"
            )

        if (
            candidate.classification.value
            in self.policy.review_kinds
        ):
            candidate.review.append(
                f"{candidate.classification.value} artifacts require review"
            )

        if candidate.path is not None:
            self._apply_path_policy(candidate)

        return candidate

    def _validate_path(
        self,
        value: object,
        candidate: _Candidate,
    ) -> str | None:
        if not isinstance(value, str) or not value:
            candidate.rejected.append(
                "path must be a non-empty string"
            )
            return None
        if value != value.strip():
            candidate.rejected.append(
                "path cannot have leading or trailing whitespace"
            )
            return None
        if len(value) > self.policy.max_path_length:
            candidate.rejected.append(
                "path exceeds policy length limit"
            )
        if "\\" in value:
            candidate.rejected.append(
                "path must use portable forward slashes"
            )
        if value.startswith("/") or _WINDOWS_DRIVE.match(value):
            candidate.rejected.append(
                "path must be relative"
            )
        if "//" in value:
            candidate.rejected.append(
                "path cannot contain empty segments"
            )
        if _CONTROL.search(value):
            candidate.rejected.append(
                "path cannot contain control characters"
            )

        path = PurePosixPath(value)
        parts = path.parts
        if (
            not parts
            or value in {".", ".."}
            or any(part in {"", ".", ".."} for part in parts)
        ):
            candidate.rejected.append(
                "path cannot contain dot or parent traversal segments"
            )

        for part in parts:
            if len(part) > self.policy.max_segment_length:
                candidate.rejected.append(
                    "path segment exceeds policy length limit"
                )
            if part.endswith((" ", ".")):
                candidate.rejected.append(
                    "path segment cannot end with space or dot"
                )
            if any(
                character in part
                for character in '<>:"|?*'
            ):
                candidate.rejected.append(
                    "path contains non-portable characters"
                )
            stem = part.split(".", 1)[0].casefold()
            if stem in _WINDOWS_RESERVED:
                candidate.rejected.append(
                    "path contains a Windows reserved name"
                )

        normalized = path.as_posix()
        if normalized != value:
            candidate.rejected.append(
                "path is not in canonical portable form"
            )
        return normalized

    def _validate_sha256(
        self,
        value: object,
        candidate: _Candidate,
    ) -> str | None:
        if not isinstance(value, str):
            candidate.rejected.append(
                "sha256 must be a string"
            )
            return None
        if _SHA256.fullmatch(value) is None:
            candidate.rejected.append(
                "sha256 must be a lowercase 64-character digest"
            )
            return None
        return value

    def _validate_size(
        self,
        value: object,
        candidate: _Candidate,
    ) -> int | None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            candidate.rejected.append(
                "size_bytes must be a non-negative integer"
            )
            return None
        if value > self.policy.max_artifact_bytes:
            candidate.rejected.append(
                "artifact size exceeds policy maximum"
            )
        return value

    def _validate_media_type(
        self,
        value: object,
        candidate: _Candidate,
    ) -> str | None:
        if not isinstance(value, str):
            candidate.rejected.append(
                "media_type must be a string"
            )
            return None
        if _MEDIA_TYPE.fullmatch(value) is None:
            candidate.rejected.append(
                "media_type must be a lowercase type/subtype token"
            )
            return None
        return value

    def _validate_kind(
        self,
        value: object,
        candidate: _Candidate,
    ) -> ArtifactClassification:
        try:
            return ArtifactClassification(value)
        except (TypeError, ValueError):
            candidate.rejected.append(
                "kind is not a supported artifact classification"
            )
            return ArtifactClassification.OTHER

    def _validate_operation(
        self,
        value: object,
        candidate: _Candidate,
    ) -> ArtifactOperation | None:
        try:
            return ArtifactOperation(value)
        except (TypeError, ValueError):
            candidate.rejected.append(
                "operation must be create or update"
            )
            return None

    def _apply_path_policy(
        self,
        candidate: _Candidate,
    ) -> None:
        assert candidate.path is not None
        lower_path = candidate.path.casefold()
        basename = PurePosixPath(lower_path).name
        suffix = PurePosixPath(lower_path).suffix

        if suffix in self.policy.forbidden_extensions:
            candidate.rejected.append(
                f"forbidden executable extension: {suffix}"
            )
        if basename in self.policy.forbidden_names:
            candidate.rejected.append(
                f"forbidden sensitive filename: {basename}"
            )
        if any(
            basename.endswith(item)
            for item in self.policy.forbidden_suffixes
        ):
            candidate.rejected.append(
                "forbidden sensitive file suffix"
            )

        if suffix in self.policy.review_extensions:
            candidate.review.append(
                f"file extension {suffix} requires review"
            )
        for prefix in self.policy.review_path_prefixes:
            if lower_path.startswith(prefix):
                candidate.review.append(
                    f"sensitive path prefix requires review: {prefix}"
                )

    def _detect_path_conflicts(
        self,
        candidates: list[_Candidate],
    ) -> None:
        groups: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            if candidate.path is not None:
                groups[candidate.path.casefold()].append(candidate)

        for group in groups.values():
            if len(group) < 2:
                continue
            paths = {item.path for item in group}
            hashes = {item.sha256 for item in group}
            if len(paths) == 1 and len(hashes) == 1:
                reason = "duplicate artifact declaration for the same path"
            else:
                reason = "conflicting artifact declarations target the same portable path"
            for candidate in group:
                candidate.rejected.append(reason)

    def _detect_content_duplicates(
        self,
        candidates: list[_Candidate],
    ) -> None:
        groups: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            if (
                candidate.sha256 is not None
                and candidate.path is not None
            ):
                groups[candidate.sha256].append(candidate)

        for group in groups.values():
            unique_paths = {
                item.path.casefold()
                for item in group
                if item.path is not None
            }
            if len(unique_paths) < 2:
                continue
            for candidate in group:
                candidate.review.append(
                    "identical content hash is declared for multiple paths"
                )
