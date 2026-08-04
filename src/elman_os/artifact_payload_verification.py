"""In-memory verification of artifact payload bytes for ELMAN-OS v0.7."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from .agent_contracts import canonical_json
from .agent_output_validation import ArtifactClassification
from .artifact_application_plan import (
    ArtifactApplicationDecision,
    ArtifactApplicationOperation,
    ArtifactApplicationPlan,
)


ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_PORTABLE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactPayloadVerificationError(ValueError):
    """A payload verification policy, request, payload, or result is invalid."""


class ArtifactPayloadVerificationIntegrityError(
    ArtifactPayloadVerificationError
):
    """A payload verification object fails an integrity check."""


class ArtifactPayloadVerificationDecision(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


class ArtifactPayloadVerificationStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires-review"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactPayloadVerificationError(
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
        raise ArtifactPayloadVerificationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactPayloadVerificationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ArtifactPayloadVerificationError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ArtifactPayloadVerificationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactPayloadVerificationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactPayloadVerificationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactPayloadVerificationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactPayloadVerificationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactPayloadVerificationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactPayloadVerificationError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _portable_relative_path(value: object, name: str) -> str:
    path = _text(value, name)
    if path != path.strip():
        raise ArtifactPayloadVerificationError(
            f"{name} cannot have surrounding whitespace"
        )
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ArtifactPayloadVerificationError(
            f"{name} must be a portable relative path"
        )
    pure = PurePosixPath(path)
    if (
        not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ArtifactPayloadVerificationError(
            f"{name} must be canonical and traversal-free"
        )
    for part in pure.parts:
        if _PORTABLE_SEGMENT.fullmatch(part) is None:
            raise ArtifactPayloadVerificationError(
                f"{name} contains a non-portable segment"
            )
    return path


def _media_type(value: object, name: str) -> str:
    media_type = _text(value, name)
    if _MEDIA_TYPE.fullmatch(media_type) is None:
        raise ArtifactPayloadVerificationError(
            f"{name} must be a lowercase type/subtype token"
        )
    return media_type


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _bytes(value: object, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    raise ArtifactPayloadVerificationError(
        f"{name} must be bytes-like"
    )


def _string_tuple(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ArtifactPayloadVerificationError(
            f"{name} must be an iterable"
        )
    try:
        raw = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ArtifactPayloadVerificationError(
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


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    operation_id: str
    destination_path: str
    media_type: str
    content: bytes = field(repr=False)
    payload_hash: str | None = None
    version: int = ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
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
        object.__setattr__(
            self,
            "media_type",
            _media_type(self.media_type, "media_type"),
        )
        object.__setattr__(
            self,
            "content",
            _bytes(self.content, "content"),
        )
        if self.version != ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION:
            raise ArtifactPayloadVerificationError(
                "unsupported artifact payload format version"
            )

        computed = self.compute_hash()
        if self.payload_hash is None:
            object.__setattr__(self, "payload_hash", computed)
        else:
            supplied = _hash(self.payload_hash, "payload_hash")
            if supplied != computed:
                raise ArtifactPayloadVerificationIntegrityError(
                    "payload hash does not match payload content"
                )
            object.__setattr__(self, "payload_hash", supplied)

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def content_base64(self) -> str:
        return base64.b64encode(self.content).decode("ascii")

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_payload",
            "version": self.version,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "media_type": self.media_type,
            "content_base64": self.content_base64,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.payload_hash != self.compute_hash():
            raise ArtifactPayloadVerificationIntegrityError(
                "payload hash does not match payload content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["payload_hash"] = self.payload_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactPayload":
        if data.get("record_type") != "artifact_payload":
            raise ArtifactPayloadVerificationError(
                "record_type must be artifact_payload"
            )
        if "payload_hash" not in data:
            raise ArtifactPayloadVerificationIntegrityError(
                "serialized payload is missing payload_hash"
            )
        encoded = data.get("content_base64")
        if not isinstance(encoded, str):
            raise ArtifactPayloadVerificationError(
                "content_base64 must be a string"
            )
        try:
            content = base64.b64decode(
                encoded,
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise ArtifactPayloadVerificationError(
                "content_base64 is invalid"
            ) from exc
        return cls(
            operation_id=data["operation_id"],
            destination_path=data["destination_path"],
            media_type=data["media_type"],
            content=content,
            payload_hash=data["payload_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactPayload":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactPayloadVerificationError(
                "artifact payload JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactPayloadVerificationError(
                "artifact payload JSON must be an object"
            )
        return cls.from_dict(data)


def _canonical_payloads(
    payloads: Iterable[ArtifactPayload],
) -> tuple[ArtifactPayload, ...]:
    try:
        values = tuple(payloads)
    except TypeError as exc:
        raise ArtifactPayloadVerificationError(
            "payloads must be iterable"
        ) from exc
    if not all(
        isinstance(item, ArtifactPayload)
        for item in values
    ):
        raise ArtifactPayloadVerificationError(
            "payloads must contain ArtifactPayload values"
        )
    for item in values:
        item.verify_hash()
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.operation_id.casefold(),
                item.destination_path.casefold(),
                item.destination_path,
                item.payload_hash or "",
            ),
        )
    )


def _payload_manifest_hash(
    payloads: tuple[ArtifactPayload, ...],
) -> str:
    return _sha256_document(
        {
            "record_type": "artifact_payload_manifest",
            "payloads": [
                item.to_dict()
                for item in payloads
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ArtifactPayloadVerificationPolicy:
    policy_id: str
    max_payloads: int = 64
    max_payload_bytes: int = 10_000_000
    max_total_bytes: int = 50_000_000
    validate_utf8_text: bool = True
    review_media_types: tuple[str, ...] = (
        "application/octet-stream",
    )
    forbidden_media_types: tuple[str, ...] = (
        "application/x-msdownload",
    )
    review_classifications: tuple[str, ...] = ()
    version: int = ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "max_payloads",
            "max_payload_bytes",
            "max_total_bytes",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        if self.max_total_bytes < self.max_payload_bytes:
            raise ArtifactPayloadVerificationError(
                "max_total_bytes cannot be smaller than max_payload_bytes"
            )
        if not isinstance(self.validate_utf8_text, bool):
            raise ArtifactPayloadVerificationError(
                "validate_utf8_text must be boolean"
            )

        review_media_types = tuple(
            sorted(
                {
                    _media_type(item, "review_media_types")
                    for item in self.review_media_types
                }
            )
        )
        forbidden_media_types = tuple(
            sorted(
                {
                    _media_type(item, "forbidden_media_types")
                    for item in self.forbidden_media_types
                }
            )
        )
        if set(review_media_types) & set(forbidden_media_types):
            raise ArtifactPayloadVerificationError(
                "review and forbidden media types overlap"
            )
        object.__setattr__(
            self,
            "review_media_types",
            review_media_types,
        )
        object.__setattr__(
            self,
            "forbidden_media_types",
            forbidden_media_types,
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
            raise ArtifactPayloadVerificationError(
                "review_classifications contains an unknown value"
            )
        object.__setattr__(
            self,
            "review_classifications",
            classifications,
        )

        if self.version != ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION:
            raise ArtifactPayloadVerificationError(
                "unsupported payload verification format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_payload_verification_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "max_payloads": self.max_payloads,
            "max_payload_bytes": self.max_payload_bytes,
            "max_total_bytes": self.max_total_bytes,
            "validate_utf8_text": self.validate_utf8_text,
            "review_media_types": list(
                self.review_media_types
            ),
            "forbidden_media_types": list(
                self.forbidden_media_types
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
    ) -> "ArtifactPayloadVerificationPolicy":
        if (
            data.get("record_type")
            != "artifact_payload_verification_policy"
        ):
            raise ArtifactPayloadVerificationError(
                "record_type must be artifact_payload_verification_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            max_payloads=data["max_payloads"],
            max_payload_bytes=data["max_payload_bytes"],
            max_total_bytes=data["max_total_bytes"],
            validate_utf8_text=data["validate_utf8_text"],
            review_media_types=tuple(
                data["review_media_types"]
            ),
            forbidden_media_types=tuple(
                data["forbidden_media_types"]
            ),
            review_classifications=tuple(
                data["review_classifications"]
            ),
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactPayloadVerificationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactPayloadVerificationError(
                "payload verification policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactPayloadVerificationError(
                "payload verification policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactPayloadVerificationRequest:
    verification_id: str
    policy_id: str
    policy_hash: str
    application_id: str
    application_plan_hash: str
    validation_result_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    requested_by: str
    requested_at: str
    payload_count: int
    payload_total_bytes: int
    payload_manifest_hash: str
    request_hash: str | None = None
    version: int = ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "verification_id",
            "policy_id",
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
            "application_plan_hash",
            "validation_result_hash",
            "payload_manifest_hash",
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
            "payload_count",
            _non_negative_int(
                self.payload_count,
                "payload_count",
            ),
        )
        object.__setattr__(
            self,
            "payload_total_bytes",
            _non_negative_int(
                self.payload_total_bytes,
                "payload_total_bytes",
            ),
        )

        if self.version != ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION:
            raise ArtifactPayloadVerificationError(
                "unsupported payload verification format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactPayloadVerificationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_plan_and_payloads(
        cls,
        plan: ArtifactApplicationPlan,
        policy: ArtifactPayloadVerificationPolicy,
        payloads: Iterable[ArtifactPayload],
        *,
        requested_by: str,
        requested_at: str | datetime,
        verification_id: str | None = None,
    ) -> "ArtifactPayloadVerificationRequest":
        if not isinstance(plan, ArtifactApplicationPlan):
            raise ArtifactPayloadVerificationError(
                "plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            policy,
            ArtifactPayloadVerificationPolicy,
        ):
            raise ArtifactPayloadVerificationError(
                "policy must be an ArtifactPayloadVerificationPolicy"
            )
        plan.verify_hash()
        normalized = _canonical_payloads(payloads)
        plan_hash = plan.plan_hash
        assert plan_hash is not None
        normalized_time = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        normalized_requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        manifest_hash = _payload_manifest_hash(normalized)
        total_bytes = sum(item.size_bytes for item in normalized)

        source_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_payload_verification_request_source"
                ),
                "policy_hash": policy.policy_hash,
                "application_id": plan.application_id,
                "application_plan_hash": plan_hash,
                "validation_result_hash": (
                    plan.validation_result_hash
                ),
                "plan_id": plan.plan_id,
                "step_id": plan.step_id,
                "agent_id": plan.agent_id,
                "requested_by": normalized_requester,
                "requested_at": normalized_time,
                "payload_count": len(normalized),
                "payload_total_bytes": total_bytes,
                "payload_manifest_hash": manifest_hash,
            }
        )
        effective_id = (
            verification_id
            if verification_id is not None
            else f"payload-verification:{source_hash}"
        )

        return cls(
            verification_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            application_id=plan.application_id,
            application_plan_hash=plan_hash,
            validation_result_hash=(
                plan.validation_result_hash
            ),
            plan_id=plan.plan_id,
            step_id=plan.step_id,
            agent_id=plan.agent_id,
            requested_by=normalized_requester,
            requested_at=normalized_time,
            payload_count=len(normalized),
            payload_total_bytes=total_bytes,
            payload_manifest_hash=manifest_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_payload_verification_request",
            "version": self.version,
            "verification_id": self.verification_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "validation_result_hash": self.validation_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "payload_count": self.payload_count,
            "payload_total_bytes": self.payload_total_bytes,
            "payload_manifest_hash": self.payload_manifest_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactPayloadVerificationIntegrityError(
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
    ) -> "ArtifactPayloadVerificationRequest":
        if (
            data.get("record_type")
            != "artifact_payload_verification_request"
        ):
            raise ArtifactPayloadVerificationError(
                "record_type must be artifact_payload_verification_request"
            )
        if "request_hash" not in data:
            raise ArtifactPayloadVerificationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            verification_id=data["verification_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            application_id=data["application_id"],
            application_plan_hash=data["application_plan_hash"],
            validation_result_hash=data[
                "validation_result_hash"
            ],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            payload_count=data["payload_count"],
            payload_total_bytes=data["payload_total_bytes"],
            payload_manifest_hash=data["payload_manifest_hash"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactPayloadVerificationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactPayloadVerificationError(
                "payload verification request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactPayloadVerificationError(
                "payload verification request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactPayloadVerificationRecord:
    index: int
    operation_sequence: int | None
    operation_id: str | None
    destination_path: str | None
    decision: ArtifactPayloadVerificationDecision
    classification: ArtifactClassification | None
    expected_sha256: str | None
    actual_sha256: str | None
    expected_size_bytes: int | None
    actual_size_bytes: int | None
    expected_media_type: str | None
    actual_media_type: str | None
    payload_hash: str | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        if self.operation_sequence is not None:
            object.__setattr__(
                self,
                "operation_sequence",
                _positive_int(
                    self.operation_sequence,
                    "operation_sequence",
                ),
            )
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                _identifier(
                    self.operation_id,
                    "operation_id",
                ),
            )
        if self.destination_path is not None:
            object.__setattr__(
                self,
                "destination_path",
                _portable_relative_path(
                    self.destination_path,
                    "destination_path",
                ),
            )
        try:
            decision = ArtifactPayloadVerificationDecision(
                self.decision
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactPayloadVerificationError(
                "payload verification decision is invalid"
            ) from exc
        object.__setattr__(self, "decision", decision)
        if self.classification is not None:
            try:
                classification = ArtifactClassification(
                    self.classification
                )
            except (TypeError, ValueError) as exc:
                raise ArtifactPayloadVerificationError(
                    "classification is invalid"
                ) from exc
            object.__setattr__(
                self,
                "classification",
                classification,
            )
        for field_name in (
            "expected_sha256",
            "actual_sha256",
            "payload_hash",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _hash(value, field_name),
                )
        for field_name in (
            "expected_size_bytes",
            "actual_size_bytes",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _non_negative_int(value, field_name),
                )
        for field_name in (
            "expected_media_type",
            "actual_media_type",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _media_type(value, field_name),
                )
        reasons = tuple(
            dict.fromkeys(
                _text(item, "reason")
                for item in self.reasons
            )
        )
        if not reasons:
            raise ArtifactPayloadVerificationError(
                "verification record must contain a reason"
            )
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "operation_sequence": self.operation_sequence,
            "operation_id": self.operation_id,
            "destination_path": self.destination_path,
            "decision": self.decision.value,
            "classification": (
                self.classification.value
                if self.classification is not None
                else None
            ),
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "expected_size_bytes": self.expected_size_bytes,
            "actual_size_bytes": self.actual_size_bytes,
            "expected_media_type": self.expected_media_type,
            "actual_media_type": self.actual_media_type,
            "payload_hash": self.payload_hash,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactPayloadVerificationRecord":
        classification = data.get("classification")
        return cls(
            index=data["index"],
            operation_sequence=data.get("operation_sequence"),
            operation_id=data.get("operation_id"),
            destination_path=data.get("destination_path"),
            decision=ArtifactPayloadVerificationDecision(
                data["decision"]
            ),
            classification=(
                ArtifactClassification(classification)
                if classification is not None
                else None
            ),
            expected_sha256=data.get("expected_sha256"),
            actual_sha256=data.get("actual_sha256"),
            expected_size_bytes=data.get(
                "expected_size_bytes"
            ),
            actual_size_bytes=data.get("actual_size_bytes"),
            expected_media_type=data.get(
                "expected_media_type"
            ),
            actual_media_type=data.get("actual_media_type"),
            payload_hash=data.get("payload_hash"),
            reasons=tuple(data["reasons"]),
        )


@dataclass(slots=True)
class _RecordCandidate:
    operation: ArtifactApplicationOperation | None
    payload_index: int | None
    payload: ArtifactPayload | None
    rejected: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)

    def to_record(
        self,
        index: int,
    ) -> ArtifactPayloadVerificationRecord:
        operation = self.operation
        payload = self.payload
        if self.rejected:
            decision = ArtifactPayloadVerificationDecision.REJECTED
            reasons = tuple(
                f"REJECTED: {item}"
                for item in dict.fromkeys(self.rejected)
            )
        elif self.review:
            decision = (
                ArtifactPayloadVerificationDecision.REQUIRES_REVIEW
            )
            reasons = tuple(
                f"REVIEW: {item}"
                for item in dict.fromkeys(self.review)
            )
        else:
            decision = ArtifactPayloadVerificationDecision.VERIFIED
            reasons = (
                "VERIFIED: payload bytes satisfy the application operation",
            )
        return ArtifactPayloadVerificationRecord(
            index=index,
            operation_sequence=(
                operation.sequence
                if operation is not None
                else None
            ),
            operation_id=(
                operation.operation_id
                if operation is not None
                else payload.operation_id
                if payload is not None
                else None
            ),
            destination_path=(
                operation.destination_path
                if operation is not None
                else payload.destination_path
                if payload is not None
                else None
            ),
            decision=decision,
            classification=(
                operation.classification
                if operation is not None
                else None
            ),
            expected_sha256=(
                operation.sha256
                if operation is not None
                else None
            ),
            actual_sha256=(
                payload.content_sha256
                if payload is not None
                else None
            ),
            expected_size_bytes=(
                operation.size_bytes
                if operation is not None
                else None
            ),
            actual_size_bytes=(
                payload.size_bytes
                if payload is not None
                else None
            ),
            expected_media_type=(
                operation.media_type
                if operation is not None
                else None
            ),
            actual_media_type=(
                payload.media_type
                if payload is not None
                else None
            ),
            payload_hash=(
                payload.payload_hash
                if payload is not None
                else None
            ),
            reasons=reasons,
        )


@dataclass(frozen=True, slots=True)
class ArtifactPayloadVerificationResult:
    verification_id: str
    status: ArtifactPayloadVerificationStatus
    request_hash: str
    policy_id: str
    policy_hash: str
    application_id: str
    application_plan_hash: str
    validation_result_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    payloads: tuple[ArtifactPayload, ...] = field(repr=False)
    records: tuple[ArtifactPayloadVerificationRecord, ...]
    top_level_reasons: tuple[str, ...]
    verified_count: int
    review_count: int
    rejected_count: int
    payload_total_bytes: int
    payload_manifest_hash: str
    verified_at: str
    result_hash: str | None = None
    version: int = ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "verification_id",
            "policy_id",
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
            status = ArtifactPayloadVerificationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactPayloadVerificationError(
                "payload verification status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        for field_name in (
            "request_hash",
            "policy_hash",
            "application_plan_hash",
            "validation_result_hash",
            "payload_manifest_hash",
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

        payloads = _canonical_payloads(self.payloads)
        object.__setattr__(self, "payloads", payloads)
        if (
            _payload_manifest_hash(payloads)
            != self.payload_manifest_hash
        ):
            raise ArtifactPayloadVerificationIntegrityError(
                "payload manifest hash does not match payloads"
            )

        records = tuple(self.records)
        if not all(
            isinstance(
                item,
                ArtifactPayloadVerificationRecord,
            )
            for item in records
        ):
            raise ArtifactPayloadVerificationError(
                "records must contain verification records"
            )
        if tuple(item.index for item in records) != tuple(
            range(len(records))
        ):
            raise ArtifactPayloadVerificationError(
                "record indexes must be contiguous from zero"
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
            "verified_count",
            "review_count",
            "rejected_count",
            "payload_total_bytes",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )

        actual_verified = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.VERIFIED
            for item in records
        )
        actual_review = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.REQUIRES_REVIEW
            for item in records
        )
        actual_rejected = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.REJECTED
            for item in records
        )
        if (
            self.verified_count,
            self.review_count,
            self.rejected_count,
        ) != (
            actual_verified,
            actual_review,
            actual_rejected,
        ):
            raise ArtifactPayloadVerificationIntegrityError(
                "verification counts do not match records"
            )
        if self.payload_total_bytes != sum(
            item.size_bytes
            for item in payloads
        ):
            raise ArtifactPayloadVerificationIntegrityError(
                "payload_total_bytes does not match payloads"
            )

        expected_status = (
            ArtifactPayloadVerificationStatus.REJECTED
            if actual_rejected
            or any(
                item.startswith("REJECTED:")
                for item in reasons
            )
            else ArtifactPayloadVerificationStatus.REQUIRES_REVIEW
            if actual_review
            or any(
                item.startswith("REVIEW:")
                for item in reasons
            )
            else ArtifactPayloadVerificationStatus.VERIFIED
        )
        if status is not expected_status:
            raise ArtifactPayloadVerificationIntegrityError(
                "verification status does not match records"
            )

        object.__setattr__(
            self,
            "verified_at",
            _utc_timestamp(self.verified_at, "verified_at"),
        )
        if self.version != ARTIFACT_PAYLOAD_VERIFICATION_FORMAT_VERSION:
            raise ArtifactPayloadVerificationError(
                "unsupported payload verification format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactPayloadVerificationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_payload_verification_result",
            "version": self.version,
            "verification_id": self.verification_id,
            "status": self.status.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "validation_result_hash": self.validation_result_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "payloads": [
                item.to_dict()
                for item in self.payloads
            ],
            "records": [
                item.to_dict()
                for item in self.records
            ],
            "top_level_reasons": list(
                self.top_level_reasons
            ),
            "verified_count": self.verified_count,
            "review_count": self.review_count,
            "rejected_count": self.rejected_count,
            "payload_total_bytes": self.payload_total_bytes,
            "payload_manifest_hash": self.payload_manifest_hash,
            "verified_at": self.verified_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactPayloadVerificationIntegrityError(
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
    ) -> "ArtifactPayloadVerificationResult":
        if (
            data.get("record_type")
            != "artifact_payload_verification_result"
        ):
            raise ArtifactPayloadVerificationError(
                "record_type must be artifact_payload_verification_result"
            )
        if "result_hash" not in data:
            raise ArtifactPayloadVerificationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            verification_id=data["verification_id"],
            status=ArtifactPayloadVerificationStatus(
                data["status"]
            ),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            application_id=data["application_id"],
            application_plan_hash=data[
                "application_plan_hash"
            ],
            validation_result_hash=data[
                "validation_result_hash"
            ],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            payloads=tuple(
                ArtifactPayload.from_dict(item)
                for item in data["payloads"]
            ),
            records=tuple(
                ArtifactPayloadVerificationRecord.from_dict(
                    item
                )
                for item in data["records"]
            ),
            top_level_reasons=tuple(
                data["top_level_reasons"]
            ),
            verified_count=data["verified_count"],
            review_count=data["review_count"],
            rejected_count=data["rejected_count"],
            payload_total_bytes=data[
                "payload_total_bytes"
            ],
            payload_manifest_hash=data[
                "payload_manifest_hash"
            ],
            verified_at=data["verified_at"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactPayloadVerificationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactPayloadVerificationError(
                "payload verification result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactPayloadVerificationError(
                "payload verification result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactPayloadVerification:
    request: ArtifactPayloadVerificationRequest
    application_plan: ArtifactApplicationPlan
    payloads: tuple[ArtifactPayload, ...]
    policy: ArtifactPayloadVerificationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactPayloadVerificationRequest,
        ):
            raise ArtifactPayloadVerificationError(
                "request must be an ArtifactPayloadVerificationRequest"
            )
        if not isinstance(
            self.application_plan,
            ArtifactApplicationPlan,
        ):
            raise ArtifactPayloadVerificationError(
                "application_plan must be an ArtifactApplicationPlan"
            )
        if not isinstance(
            self.policy,
            ArtifactPayloadVerificationPolicy,
        ):
            raise ArtifactPayloadVerificationError(
                "policy must be an ArtifactPayloadVerificationPolicy"
            )
        normalized = _canonical_payloads(self.payloads)
        object.__setattr__(self, "payloads", normalized)

        self.request.verify_hash()
        self.application_plan.verify_hash()
        plan_hash = self.application_plan.plan_hash
        assert plan_hash is not None

        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "application_id": (
                self.application_plan.application_id
            ),
            "application_plan_hash": plan_hash,
            "validation_result_hash": (
                self.application_plan.validation_result_hash
            ),
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
            "payload_count": len(normalized),
            "payload_total_bytes": sum(
                item.size_bytes
                for item in normalized
            ),
            "payload_manifest_hash": (
                _payload_manifest_hash(normalized)
            ),
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactPayloadVerificationError(
                    f"request {field_name} does not match verification source"
                )

    def verify(self) -> ArtifactPayloadVerificationResult:
        self.request.verify_hash()
        self.application_plan.verify_hash()
        payloads = self.payloads
        top_rejected: list[str] = []
        top_review: list[str] = []

        if (
            self.application_plan.decision
            is not ArtifactApplicationDecision.READY
        ):
            top_rejected.append(
                "application plan decision must be ready"
            )
        if not self.application_plan.operations:
            top_rejected.append(
                "application plan contains no operations"
            )
        if len(payloads) > self.policy.max_payloads:
            top_rejected.append(
                "payload count exceeds policy maximum"
            )
        total_bytes = sum(item.size_bytes for item in payloads)
        if total_bytes > self.policy.max_total_bytes:
            top_rejected.append(
                "payload total size exceeds policy maximum"
            )

        by_operation: dict[str, list[int]] = defaultdict(list)
        for index, payload in enumerate(payloads):
            by_operation[payload.operation_id].append(index)

        candidates: list[_RecordCandidate] = []
        selected_indexes: set[int] = set()

        for operation in self.application_plan.operations:
            matches = by_operation.get(
                operation.operation_id,
                [],
            )
            if not matches:
                candidate = _RecordCandidate(
                    operation=operation,
                    payload_index=None,
                    payload=None,
                )
                candidate.rejected.append(
                    "payload is missing for application operation"
                )
                candidates.append(candidate)
                continue

            selected = matches[0]
            selected_indexes.add(selected)
            candidate = _RecordCandidate(
                operation=operation,
                payload_index=selected,
                payload=payloads[selected],
            )
            if len(matches) > 1:
                candidate.rejected.append(
                    "multiple payloads declare the same operation_id"
                )
            self._validate_payload(candidate)
            candidates.append(candidate)

            for duplicate_index in matches[1:]:
                selected_indexes.add(duplicate_index)
                duplicate = _RecordCandidate(
                    operation=None,
                    payload_index=duplicate_index,
                    payload=payloads[duplicate_index],
                )
                duplicate.rejected.append(
                    "duplicate payload for an existing operation_id"
                )
                candidates.append(duplicate)

        expected_ids = {
            item.operation_id
            for item in self.application_plan.operations
        }
        for index, payload in enumerate(payloads):
            if index in selected_indexes:
                continue
            extra = _RecordCandidate(
                operation=None,
                payload_index=index,
                payload=payload,
            )
            if payload.operation_id not in expected_ids:
                extra.rejected.append(
                    "payload references an unknown operation_id"
                )
            else:
                extra.rejected.append(
                    "payload is an unselected duplicate"
                )
            candidates.append(extra)

        self._detect_destination_conflicts(candidates)

        records = tuple(
            candidate.to_record(index)
            for index, candidate in enumerate(candidates)
        )
        verified_count = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.VERIFIED
            for item in records
        )
        review_count = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.REQUIRES_REVIEW
            for item in records
        )
        rejected_count = sum(
            item.decision
            is ArtifactPayloadVerificationDecision.REJECTED
            for item in records
        )

        top_level_reasons = tuple(
            [
                *(
                    f"REJECTED: {item}"
                    for item in dict.fromkeys(top_rejected)
                ),
                *(
                    f"REVIEW: {item}"
                    for item in dict.fromkeys(top_review)
                ),
            ]
        )
        status = (
            ArtifactPayloadVerificationStatus.REJECTED
            if rejected_count or top_rejected
            else ArtifactPayloadVerificationStatus.REQUIRES_REVIEW
            if review_count or top_review
            else ArtifactPayloadVerificationStatus.VERIFIED
        )

        request_hash = self.request.request_hash
        plan_hash = self.application_plan.plan_hash
        assert request_hash is not None
        assert plan_hash is not None

        return ArtifactPayloadVerificationResult(
            verification_id=self.request.verification_id,
            status=status,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            application_id=(
                self.application_plan.application_id
            ),
            application_plan_hash=plan_hash,
            validation_result_hash=(
                self.application_plan.validation_result_hash
            ),
            plan_id=self.application_plan.plan_id,
            step_id=self.application_plan.step_id,
            agent_id=self.application_plan.agent_id,
            payloads=payloads,
            records=records,
            top_level_reasons=top_level_reasons,
            verified_count=verified_count,
            review_count=review_count,
            rejected_count=rejected_count,
            payload_total_bytes=total_bytes,
            payload_manifest_hash=(
                self.request.payload_manifest_hash
            ),
            verified_at=self.request.requested_at,
        )

    def _validate_payload(
        self,
        candidate: _RecordCandidate,
    ) -> None:
        operation = candidate.operation
        payload = candidate.payload
        assert operation is not None
        assert payload is not None

        if payload.destination_path != operation.destination_path:
            candidate.rejected.append(
                "payload destination_path does not match operation"
            )
        if payload.media_type != operation.media_type:
            candidate.rejected.append(
                "payload media_type does not match operation"
            )
        if payload.size_bytes != operation.size_bytes:
            candidate.rejected.append(
                "payload byte length does not match operation"
            )
        if payload.content_sha256 != operation.sha256:
            candidate.rejected.append(
                "payload SHA-256 does not match operation"
            )
        if payload.size_bytes > self.policy.max_payload_bytes:
            candidate.rejected.append(
                "payload size exceeds policy maximum"
            )
        if payload.media_type in self.policy.forbidden_media_types:
            candidate.rejected.append(
                "payload media type is forbidden by policy"
            )
        if payload.media_type in self.policy.review_media_types:
            candidate.review.append(
                "payload media type requires human review"
            )
        if (
            operation.classification.value
            in self.policy.review_classifications
        ):
            candidate.review.append(
                "artifact classification requires human review"
            )
        if (
            self.policy.validate_utf8_text
            and payload.media_type.startswith("text/")
        ):
            try:
                payload.content.decode("utf-8")
            except UnicodeDecodeError:
                candidate.rejected.append(
                    "text payload is not valid UTF-8"
                )

    def _detect_destination_conflicts(
        self,
        candidates: list[_RecordCandidate],
    ) -> None:
        groups: dict[str, list[_RecordCandidate]] = defaultdict(list)
        for candidate in candidates:
            payload = candidate.payload
            if payload is not None:
                groups[
                    payload.destination_path.casefold()
                ].append(candidate)
        for group in groups.values():
            if len(group) < 2:
                continue
            for candidate in group:
                candidate.rejected.append(
                    "multiple payloads target the same portable destination"
                )
