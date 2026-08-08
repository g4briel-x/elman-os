"""Fail-closed final verification contracts for ELMAN-OS v0.7.

The verifier consumes immutable snapshots produced by the orchestration,
artifact, metacognitive, and project-memory boundaries.  It never executes
generated code, changes project files, reads environment variables, or
performs network access.

Every final report is emitted with an HMAC-SHA-256 signature.  A rejected
execution is signed too, so the denial remains auditable and cannot be
silently rewritten as a successful completion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .agent_contracts import canonical_json
from .agent_output_validation import (
    AgentOutputValidationResult,
    AgentOutputValidationStatus,
)
from .artifact_payload_verification import (
    ArtifactPayloadVerificationResult,
    ArtifactPayloadVerificationStatus,
)
from .execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
)
from .execution_plan import (
    ExecutionPlan,
    PlanStatus,
    StepStatus,
)
from .metacognitive_supervision_decision_contracts import (
    MetacognitiveDecisionAction,
    MetacognitiveSupervisionDecision,
)
from .project_memory import (
    ProjectMemoryKind,
    ProjectMemoryRecord,
    ProjectMemoryState,
)


FINAL_VERIFICATION_FORMAT_VERSION: Final[int] = 1
FINAL_REPORT_SIGNATURE_ALGORITHM: Final[str] = "hmac-sha256"
FINAL_REPORT_MINIMUM_KEY_BYTES: Final[int] = 32

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FinalVerificationError(ValueError):
    """A final-verification contract or operation is invalid."""


class FinalVerificationIntegrityError(FinalVerificationError):
    """A hash-bound final-verification object failed validation."""


class FinalVerificationPolicyError(FinalVerificationError):
    """A final-verification policy weakens a mandatory safety gate."""


class FinalReportSignatureError(FinalVerificationIntegrityError):
    """A final report is unsigned or its signature is invalid."""


class FinalVerificationStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"


class FinalEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    REQUIRES_REVIEW = "requires-review"


class FinalEvidenceKind(StrEnum):
    OUTPUT_VALIDATION = "output-validation"
    ARTIFACT_INTEGRITY = "artifact-integrity"
    TEST_RESULT = "test-result"
    APPROVAL = "approval"
    POLICY_CHECK = "policy-check"
    DECISION_OUTCOME = "decision-outcome"
    EXTERNAL = "external"


class FinalVerificationGate(StrEnum):
    PLAN_COMPLETION = "plan-completion"
    JOURNAL_INTEGRITY = "journal-integrity"
    OUTPUT_VALIDATION = "output-validation"
    ARTIFACT_INTEGRITY = "artifact-integrity"
    EVIDENCE_COMPLETENESS = "evidence-completeness"
    POLICY_COMPLIANCE = "policy-compliance"
    ERROR_RESOLUTION = "error-resolution"
    DECISION_COHERENCE = "decision-coherence"
    SUPERVISION_CLEARANCE = "supervision-clearance"


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalVerificationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise FinalVerificationError(f"{name} exceeds {maximum} characters")
    if any(
        ord(character) < 32 and character not in "\n\r\t"
        for character in result
    ):
        raise FinalVerificationError(f"{name} contains control characters")
    return result


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name, maximum=192)
    if pattern.fullmatch(result) is None:
        raise FinalVerificationError(f"{name} has an invalid format")
    return result


def _optional_identifier(
    value: object | None,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str | None:
    return None if value is None else _identifier(value, name, pattern)


def _hash(value: object, name: str) -> str:
    result = _text(value, name, maximum=64)
    if _SHA256.fullmatch(result) is None:
        raise FinalVerificationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise FinalVerificationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise FinalVerificationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
    else:
        raise FinalVerificationError(
            f"{name} must be a UTC datetime or string"
        )
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise FinalVerificationError(f"{name} must already be UTC")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise FinalVerificationError(f"{name} must be boolean")
    return value


def _positive_int(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FinalVerificationError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise FinalVerificationError(f"{name} cannot exceed {maximum}")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinalVerificationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _plan_state_hash(plan: ExecutionPlan) -> str:
    return _sha256_document(plan.to_dict())


def _canonical_json_object(payload: object, name: str) -> str:
    text = _text(payload, name, maximum=16_777_216)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FinalVerificationError(f"{name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise FinalVerificationError(f"{name} must contain a JSON object")
    return canonical_json(data)


def _identifiers(
    values: Iterable[object],
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise FinalVerificationError(f"{name} must be an iterable")
    normalized = tuple(
        sorted({_identifier(value, name, pattern) for value in values})
    )
    if required and not normalized:
        raise FinalVerificationError(f"{name} must not be empty")
    return normalized


def _unique_by(
    values: Iterable[Any],
    attribute: str,
    name: str,
) -> tuple[Any, ...]:
    normalized = tuple(values)
    keys = [getattr(value, attribute) for value in normalized]
    duplicate = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicate:
        raise FinalVerificationError(
            f"{name} contains duplicate identifiers: {', '.join(duplicate)}"
        )
    return tuple(sorted(normalized, key=lambda value: getattr(value, attribute)))


@dataclass(frozen=True, slots=True)
class FinalVerificationPolicy:
    """Immutable policy whose mandatory gates cannot be disabled."""

    policy_id: str
    minimum_verified_evidence_per_step: int = 1
    require_current_supervision: bool = True
    require_memory_decision_links: bool = True
    require_terminal_journal: bool = True
    require_signed_report: bool = True
    fail_closed: bool = True
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        object.__setattr__(
            self,
            "minimum_verified_evidence_per_step",
            _positive_int(
                self.minimum_verified_evidence_per_step,
                "minimum_verified_evidence_per_step",
                maximum=16,
            ),
        )
        for name in (
            "require_current_supervision",
            "require_memory_decision_links",
            "require_terminal_journal",
            "require_signed_report",
            "fail_closed",
        ):
            value = _boolean(getattr(self, name), name)
            object.__setattr__(self, name, value)
            if not value:
                raise FinalVerificationPolicyError(
                    f"{name} is mandatory for fail-closed final verification"
                )
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError(
                "unsupported final-verification policy version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "final_verification_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "minimum_verified_evidence_per_step": (
                self.minimum_verified_evidence_per_step
            ),
            "require_current_supervision": self.require_current_supervision,
            "require_memory_decision_links": (
                self.require_memory_decision_links
            ),
            "require_terminal_journal": self.require_terminal_journal,
            "require_signed_report": self.require_signed_report,
            "fail_closed": self.fail_closed,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def policy_hash(self) -> str:
        return _sha256_document(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalVerificationPolicy":
        if data.get("record_type") != "final_verification_policy":
            raise FinalVerificationError(
                "record_type must be final_verification_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            minimum_verified_evidence_per_step=data[
                "minimum_verified_evidence_per_step"
            ],
            require_current_supervision=data["require_current_supervision"],
            require_memory_decision_links=data[
                "require_memory_decision_links"
            ],
            require_terminal_journal=data["require_terminal_journal"],
            require_signed_report=data["require_signed_report"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalVerificationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FinalVerificationError(
                "final-verification policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise FinalVerificationError(
                "final-verification policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class FinalEvidenceRecord:
    evidence_id: str
    kind: FinalEvidenceKind
    status: FinalEvidenceStatus
    plan_id: str
    source_reference: str
    source_hash: str
    captured_at: str
    step_id: str | None = None
    evidence_hash: str | None = None
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_id", _identifier(self.evidence_id, "evidence_id")
        )
        try:
            kind = FinalEvidenceKind(self.kind)
            status = FinalEvidenceStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise FinalVerificationError("evidence kind or status is invalid") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "plan_id", _identifier(self.plan_id, "plan_id"))
        object.__setattr__(
            self,
            "source_reference",
            _identifier(self.source_reference, "source_reference"),
        )
        object.__setattr__(
            self, "source_hash", _hash(self.source_hash, "source_hash")
        )
        object.__setattr__(
            self, "captured_at", _timestamp(self.captured_at, "captured_at")
        )
        object.__setattr__(
            self,
            "step_id",
            _optional_identifier(self.step_id, "step_id", _STEP_ID),
        )
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported evidence record version")
        computed = self.compute_hash()
        if self.evidence_hash is None:
            object.__setattr__(self, "evidence_hash", computed)
        elif _hash(self.evidence_hash, "evidence_hash") != computed:
            raise FinalVerificationIntegrityError(
                "evidence_hash does not match evidence content"
            )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_evidence_record",
            "version": self.version,
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "source_reference": self.source_reference,
            "source_hash": self.source_hash,
            "captured_at": self.captured_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.evidence_hash != self.compute_hash():
            raise FinalVerificationIntegrityError(
                "evidence_hash does not match evidence content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["evidence_hash"] = self.evidence_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalEvidenceRecord":
        if data.get("record_type") != "final_evidence_record":
            raise FinalVerificationError(
                "record_type must be final_evidence_record"
            )
        if "evidence_hash" not in data:
            raise FinalVerificationIntegrityError(
                "serialized evidence is missing evidence_hash"
            )
        return cls(
            evidence_id=data["evidence_id"],
            kind=data["kind"],
            status=data["status"],
            plan_id=data["plan_id"],
            step_id=data.get("step_id"),
            source_reference=data["source_reference"],
            source_hash=data["source_hash"],
            captured_at=data["captured_at"],
            evidence_hash=data["evidence_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalEvidenceRecord":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "evidence_json")))


@dataclass(frozen=True, slots=True)
class FinalPolicyFinding:
    finding_id: str
    rule_id: str
    summary: str
    resolved: bool
    detected_at: str
    resolution_evidence_id: str | None = None
    finding_hash: str | None = None
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _identifier(self.finding_id, "finding_id"))
        object.__setattr__(self, "rule_id", _identifier(self.rule_id, "rule_id", _TOKEN))
        object.__setattr__(self, "summary", _text(self.summary, "summary", maximum=2048))
        object.__setattr__(self, "resolved", _boolean(self.resolved, "resolved"))
        object.__setattr__(
            self, "detected_at", _timestamp(self.detected_at, "detected_at")
        )
        reference = _optional_identifier(
            self.resolution_evidence_id,
            "resolution_evidence_id",
        )
        if self.resolved != (reference is not None):
            raise FinalVerificationError(
                "resolved policy findings require exactly one resolution evidence"
            )
        object.__setattr__(self, "resolution_evidence_id", reference)
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported policy finding version")
        computed = self.compute_hash()
        if self.finding_hash is None:
            object.__setattr__(self, "finding_hash", computed)
        elif _hash(self.finding_hash, "finding_hash") != computed:
            raise FinalVerificationIntegrityError(
                "finding_hash does not match policy finding"
            )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_policy_finding",
            "version": self.version,
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "summary": self.summary,
            "resolved": self.resolved,
            "resolution_evidence_id": self.resolution_evidence_id,
            "detected_at": self.detected_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.finding_hash != self.compute_hash():
            raise FinalVerificationIntegrityError("policy finding hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["finding_hash"] = self.finding_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalPolicyFinding":
        if data.get("record_type") != "final_policy_finding":
            raise FinalVerificationError(
                "record_type must be final_policy_finding"
            )
        if "finding_hash" not in data:
            raise FinalVerificationIntegrityError(
                "serialized policy finding is missing finding_hash"
            )
        return cls(
            finding_id=data["finding_id"],
            rule_id=data["rule_id"],
            summary=data["summary"],
            resolved=data["resolved"],
            resolution_evidence_id=data.get("resolution_evidence_id"),
            detected_at=data["detected_at"],
            finding_hash=data["finding_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalPolicyFinding":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "finding_json")))


@dataclass(frozen=True, slots=True)
class FinalExecutionErrorRecord:
    error_id: str
    code: str
    summary: str
    resolved: bool
    detected_at: str
    step_id: str | None = None
    resolution_evidence_id: str | None = None
    error_hash: str | None = None
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "error_id", _identifier(self.error_id, "error_id"))
        object.__setattr__(self, "code", _identifier(self.code, "code", _TOKEN))
        object.__setattr__(self, "summary", _text(self.summary, "summary", maximum=2048))
        object.__setattr__(self, "resolved", _boolean(self.resolved, "resolved"))
        object.__setattr__(
            self, "detected_at", _timestamp(self.detected_at, "detected_at")
        )
        object.__setattr__(
            self,
            "step_id",
            _optional_identifier(self.step_id, "step_id", _STEP_ID),
        )
        reference = _optional_identifier(
            self.resolution_evidence_id,
            "resolution_evidence_id",
        )
        if self.resolved != (reference is not None):
            raise FinalVerificationError(
                "resolved execution errors require exactly one resolution evidence"
            )
        object.__setattr__(self, "resolution_evidence_id", reference)
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported execution error version")
        computed = self.compute_hash()
        if self.error_hash is None:
            object.__setattr__(self, "error_hash", computed)
        elif _hash(self.error_hash, "error_hash") != computed:
            raise FinalVerificationIntegrityError(
                "error_hash does not match execution error"
            )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_execution_error",
            "version": self.version,
            "error_id": self.error_id,
            "code": self.code,
            "summary": self.summary,
            "resolved": self.resolved,
            "resolution_evidence_id": self.resolution_evidence_id,
            "detected_at": self.detected_at,
            "step_id": self.step_id,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.error_hash != self.compute_hash():
            raise FinalVerificationIntegrityError("execution error hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["error_hash"] = self.error_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalExecutionErrorRecord":
        if data.get("record_type") != "final_execution_error":
            raise FinalVerificationError(
                "record_type must be final_execution_error"
            )
        if "error_hash" not in data:
            raise FinalVerificationIntegrityError(
                "serialized execution error is missing error_hash"
            )
        return cls(
            error_id=data["error_id"],
            code=data["code"],
            summary=data["summary"],
            resolved=data["resolved"],
            resolution_evidence_id=data.get("resolution_evidence_id"),
            detected_at=data["detected_at"],
            step_id=data.get("step_id"),
            error_hash=data["error_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalExecutionErrorRecord":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "error_json")))


@dataclass(frozen=True, slots=True)
class FinalDecisionOutcomeLink:
    link_id: str
    memory_id: str
    memory_revision_hash: str
    expected_result_hash: str
    observed_result_hash: str
    evidence_ids: tuple[str, ...]
    linked_at: str
    link_hash: str | None = None
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "link_id", _identifier(self.link_id, "link_id"))
        object.__setattr__(self, "memory_id", _identifier(self.memory_id, "memory_id"))
        for name in (
            "memory_revision_hash",
            "expected_result_hash",
            "observed_result_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        object.__setattr__(
            self,
            "evidence_ids",
            _identifiers(self.evidence_ids, "evidence_id", required=True),
        )
        object.__setattr__(self, "linked_at", _timestamp(self.linked_at, "linked_at"))
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported decision outcome link version")
        computed = self.compute_hash()
        if self.link_hash is None:
            object.__setattr__(self, "link_hash", computed)
        elif _hash(self.link_hash, "link_hash") != computed:
            raise FinalVerificationIntegrityError(
                "link_hash does not match decision outcome link"
            )

    @property
    def coherent(self) -> bool:
        return hmac.compare_digest(
            self.expected_result_hash,
            self.observed_result_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_decision_outcome_link",
            "version": self.version,
            "link_id": self.link_id,
            "memory_id": self.memory_id,
            "memory_revision_hash": self.memory_revision_hash,
            "expected_result_hash": self.expected_result_hash,
            "observed_result_hash": self.observed_result_hash,
            "evidence_ids": list(self.evidence_ids),
            "linked_at": self.linked_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.link_hash != self.compute_hash():
            raise FinalVerificationIntegrityError("decision outcome link hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["link_hash"] = self.link_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalDecisionOutcomeLink":
        if data.get("record_type") != "final_decision_outcome_link":
            raise FinalVerificationError(
                "record_type must be final_decision_outcome_link"
            )
        if "link_hash" not in data:
            raise FinalVerificationIntegrityError(
                "serialized decision outcome link is missing link_hash"
            )
        return cls(
            link_id=data["link_id"],
            memory_id=data["memory_id"],
            memory_revision_hash=data["memory_revision_hash"],
            expected_result_hash=data["expected_result_hash"],
            observed_result_hash=data["observed_result_hash"],
            evidence_ids=tuple(data["evidence_ids"]),
            linked_at=data["linked_at"],
            link_hash=data["link_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalDecisionOutcomeLink":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "link_json")))


def _evidence_for_output(
    result: AgentOutputValidationResult,
) -> FinalEvidenceRecord:
    assert result.result_hash is not None
    status = {
        AgentOutputValidationStatus.ACCEPTED: FinalEvidenceStatus.VERIFIED,
        AgentOutputValidationStatus.REJECTED: FinalEvidenceStatus.FAILED,
        AgentOutputValidationStatus.REQUIRES_REVIEW: (
            FinalEvidenceStatus.REQUIRES_REVIEW
        ),
    }[result.status]
    return FinalEvidenceRecord(
        evidence_id=f"evidence:output:{result.validation_id}",
        kind=FinalEvidenceKind.OUTPUT_VALIDATION,
        status=status,
        plan_id=result.plan_id,
        step_id=result.step_id,
        source_reference=result.validation_id,
        source_hash=result.result_hash,
        captured_at=result.validated_at,
    )


def _evidence_for_payload(
    result: ArtifactPayloadVerificationResult,
) -> FinalEvidenceRecord:
    assert result.result_hash is not None
    status = {
        ArtifactPayloadVerificationStatus.VERIFIED: FinalEvidenceStatus.VERIFIED,
        ArtifactPayloadVerificationStatus.REJECTED: FinalEvidenceStatus.FAILED,
        ArtifactPayloadVerificationStatus.REQUIRES_REVIEW: (
            FinalEvidenceStatus.REQUIRES_REVIEW
        ),
    }[result.status]
    return FinalEvidenceRecord(
        evidence_id=f"evidence:payload:{result.verification_id}",
        kind=FinalEvidenceKind.ARTIFACT_INTEGRITY,
        status=status,
        plan_id=result.plan_id,
        step_id=result.step_id,
        source_reference=result.verification_id,
        source_hash=result.result_hash,
        captured_at=result.verified_at,
    )


@dataclass(frozen=True, slots=True)
class FinalVerificationRequest:
    verification_id: str
    policy_json: str
    policy_hash: str
    plan_json: str
    plan_state_hash: str
    journal_jsonl: str
    journal_hash: str
    journal_head_hash: str
    journal_event_count: int
    output_validation_jsons: tuple[str, ...]
    payload_verification_jsons: tuple[str, ...]
    evidence_jsons: tuple[str, ...]
    policy_finding_jsons: tuple[str, ...]
    execution_error_jsons: tuple[str, ...]
    decision_link_jsons: tuple[str, ...]
    supervision_decision_jsons: tuple[str, ...]
    memory_record_jsons: tuple[str, ...]
    verifier_id: str
    requested_at: str
    request_hash: str | None = None
    version: int = FINAL_VERIFICATION_FORMAT_VERSION
    _plan: ExecutionPlan = field(init=False, repr=False, compare=False)
    _journal: ExecutionJournal = field(init=False, repr=False, compare=False)
    _policy: FinalVerificationPolicy = field(init=False, repr=False, compare=False)
    _outputs: tuple[AgentOutputValidationResult, ...] = field(
        init=False, repr=False, compare=False
    )
    _payloads: tuple[ArtifactPayloadVerificationResult, ...] = field(
        init=False, repr=False, compare=False
    )
    _evidence: tuple[FinalEvidenceRecord, ...] = field(
        init=False, repr=False, compare=False
    )
    _policy_findings: tuple[FinalPolicyFinding, ...] = field(
        init=False, repr=False, compare=False
    )
    _execution_errors: tuple[FinalExecutionErrorRecord, ...] = field(
        init=False, repr=False, compare=False
    )
    _decision_links: tuple[FinalDecisionOutcomeLink, ...] = field(
        init=False, repr=False, compare=False
    )
    _supervision_decisions: tuple[MetacognitiveSupervisionDecision, ...] = field(
        init=False, repr=False, compare=False
    )
    _memory_records: tuple[ProjectMemoryRecord, ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "verification_id", _identifier(self.verification_id, "verification_id")
        )
        policy = FinalVerificationPolicy.from_json(
            _text(self.policy_json, "policy_json", maximum=65_536)
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise FinalVerificationIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)
        object.__setattr__(self, "_policy", policy)

        try:
            plan = ExecutionPlan.from_json(
                _text(self.plan_json, "plan_json", maximum=16_777_216)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "embedded execution plan is invalid"
            ) from exc
        computed_plan_hash = _plan_state_hash(plan)
        if _hash(self.plan_state_hash, "plan_state_hash") != computed_plan_hash:
            raise FinalVerificationIntegrityError(
                "plan_state_hash does not match embedded plan"
            )
        object.__setattr__(self, "plan_json", plan.to_json())
        object.__setattr__(self, "plan_state_hash", computed_plan_hash)
        object.__setattr__(self, "_plan", plan)

        try:
            journal = ExecutionJournal.from_jsonl(
                _text(self.journal_jsonl, "journal_jsonl", maximum=64_000_000),
                expected_plan_id=plan.plan_id,
                expected_event_count=_non_negative_int(
                    self.journal_event_count,
                    "journal_event_count",
                ),
                expected_head_hash=_hash(
                    self.journal_head_hash,
                    "journal_head_hash",
                ),
                expected_journal_hash=_hash(self.journal_hash, "journal_hash"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "embedded execution journal is invalid"
            ) from exc
        seal = journal.seal()
        object.__setattr__(self, "journal_jsonl", journal.to_jsonl())
        object.__setattr__(self, "journal_event_count", seal.event_count)
        object.__setattr__(self, "journal_head_hash", seal.head_hash)
        object.__setattr__(self, "journal_hash", seal.journal_hash)
        object.__setattr__(self, "_journal", journal)

        outputs = self._decode_outputs(self.output_validation_jsons)
        payloads = self._decode_payloads(self.payload_verification_jsons)
        evidence = self._decode_records(
            self.evidence_jsons,
            FinalEvidenceRecord.from_json,
            "evidence_id",
            "evidence records",
        )
        findings = self._decode_records(
            self.policy_finding_jsons,
            FinalPolicyFinding.from_json,
            "finding_id",
            "policy findings",
        )
        errors = self._decode_records(
            self.execution_error_jsons,
            FinalExecutionErrorRecord.from_json,
            "error_id",
            "execution errors",
        )
        links = self._decode_records(
            self.decision_link_jsons,
            FinalDecisionOutcomeLink.from_json,
            "link_id",
            "decision links",
        )
        supervision = self._decode_supervision(self.supervision_decision_jsons)
        memory = self._decode_memory(self.memory_record_jsons)

        object.__setattr__(
            self,
            "output_validation_jsons",
            tuple(item.to_json() for item in outputs),
        )
        object.__setattr__(
            self,
            "payload_verification_jsons",
            tuple(item.to_json() for item in payloads),
        )
        object.__setattr__(
            self, "evidence_jsons", tuple(item.to_json() for item in evidence)
        )
        object.__setattr__(
            self,
            "policy_finding_jsons",
            tuple(item.to_json() for item in findings),
        )
        object.__setattr__(
            self,
            "execution_error_jsons",
            tuple(item.to_json() for item in errors),
        )
        object.__setattr__(
            self,
            "decision_link_jsons",
            tuple(item.to_json() for item in links),
        )
        object.__setattr__(
            self,
            "supervision_decision_jsons",
            tuple(item.to_json() for item in supervision),
        )
        object.__setattr__(
            self,
            "memory_record_jsons",
            tuple(item.to_json() for item in memory),
        )
        object.__setattr__(self, "_outputs", outputs)
        object.__setattr__(self, "_payloads", payloads)
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_policy_findings", findings)
        object.__setattr__(self, "_execution_errors", errors)
        object.__setattr__(self, "_decision_links", links)
        object.__setattr__(self, "_supervision_decisions", supervision)
        object.__setattr__(self, "_memory_records", memory)

        object.__setattr__(
            self, "verifier_id", _identifier(self.verifier_id, "verifier_id", _AGENT_ID)
        )
        object.__setattr__(
            self, "requested_at", _timestamp(self.requested_at, "requested_at")
        )
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported final-verification request version")

        self._validate_bindings()
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        elif _hash(self.request_hash, "request_hash") != computed:
            raise FinalVerificationIntegrityError(
                "request_hash does not match request content"
            )

    @staticmethod
    def _decode_records(
        payloads: Iterable[str],
        loader: Any,
        identifier: str,
        name: str,
    ) -> tuple[Any, ...]:
        if isinstance(payloads, (str, bytes)):
            raise FinalVerificationError(f"{name} must be an iterable")
        try:
            values = tuple(loader(payload) for payload in payloads)
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(f"{name} are invalid") from exc
        return _unique_by(values, identifier, name)

    @staticmethod
    def _decode_outputs(
        payloads: Iterable[str],
    ) -> tuple[AgentOutputValidationResult, ...]:
        if isinstance(payloads, (str, bytes)):
            raise FinalVerificationError(
                "output_validation_jsons must be an iterable"
            )
        try:
            values = tuple(
                AgentOutputValidationResult.from_json(payload)
                for payload in payloads
            )
            for value in values:
                value.verify_hash()
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "output validation results are invalid"
            ) from exc
        return _unique_by(values, "validation_id", "output validation results")

    @staticmethod
    def _decode_payloads(
        payloads: Iterable[str],
    ) -> tuple[ArtifactPayloadVerificationResult, ...]:
        if isinstance(payloads, (str, bytes)):
            raise FinalVerificationError(
                "payload_verification_jsons must be an iterable"
            )
        try:
            values = tuple(
                ArtifactPayloadVerificationResult.from_json(payload)
                for payload in payloads
            )
            for value in values:
                value.verify_hash()
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "payload verification results are invalid"
            ) from exc
        return _unique_by(values, "verification_id", "payload verification results")

    @staticmethod
    def _decode_supervision(
        payloads: Iterable[str],
    ) -> tuple[MetacognitiveSupervisionDecision, ...]:
        if isinstance(payloads, (str, bytes)):
            raise FinalVerificationError(
                "supervision_decision_jsons must be an iterable"
            )
        try:
            values = tuple(
                MetacognitiveSupervisionDecision.from_json(payload)
                for payload in payloads
            )
            for value in values:
                value.verify_hash()
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "supervision decisions are invalid"
            ) from exc
        return _unique_by(values, "decision_id", "supervision decisions")

    @staticmethod
    def _decode_memory(
        payloads: Iterable[str],
    ) -> tuple[ProjectMemoryRecord, ...]:
        if isinstance(payloads, (str, bytes)):
            raise FinalVerificationError("memory_record_jsons must be an iterable")
        try:
            values = tuple(ProjectMemoryRecord.from_json(payload) for payload in payloads)
            for value in values:
                value.verify_hash()
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalVerificationIntegrityError(
                "project-memory records are invalid"
            ) from exc
        keys = [(value.memory_id, value.revision) for value in values]
        if len(keys) != len(set(keys)):
            raise FinalVerificationError("project-memory records contain duplicates")
        return tuple(sorted(values, key=lambda value: (value.memory_id, value.revision)))

    def _validate_bindings(self) -> None:
        plan = self._plan
        step_by_id = {step.step_id: step for step in plan.steps}
        journal = self._journal

        output_hashes: dict[str, AgentOutputValidationResult] = {}
        known_sources: dict[str, str] = {
            f"journal:{plan.plan_id}": self.journal_hash,
            f"plan:{plan.plan_id}": self.plan_state_hash,
        }
        for result in self._outputs:
            if result.plan_id != plan.plan_id or result.step_id not in step_by_id:
                raise FinalVerificationIntegrityError(
                    "output validation is not bound to the embedded plan"
                )
            step = step_by_id[result.step_id]
            if (
                step.assigned_agent_id is not None
                and result.agent_id != step.assigned_agent_id
            ):
                raise FinalVerificationIntegrityError(
                    "output validation agent does not match final plan"
                )
            if not (1 <= result.journal_event_count <= journal.event_count):
                raise FinalVerificationIntegrityError(
                    "output validation journal prefix is out of range"
                )
            prefix = ExecutionJournal.from_events(
                plan.plan_id,
                journal.events[: result.journal_event_count],
            ).seal()
            if (
                prefix.head_hash != result.journal_head_hash
                or prefix.journal_hash != result.journal_hash
            ):
                raise FinalVerificationIntegrityError(
                    "output validation is not bound to a journal prefix"
                )
            assert result.result_hash is not None
            output_hashes[result.result_hash] = result
            known_sources[result.validation_id] = result.result_hash

        for result in self._payloads:
            if result.plan_id != plan.plan_id or result.step_id not in step_by_id:
                raise FinalVerificationIntegrityError(
                    "payload verification is not bound to the embedded plan"
                )
            source = output_hashes.get(result.validation_result_hash)
            if source is None:
                raise FinalVerificationIntegrityError(
                    "payload verification references an unknown output result"
                )
            if source.step_id != result.step_id or source.agent_id != result.agent_id:
                raise FinalVerificationIntegrityError(
                    "payload verification source identity does not match"
                )
            assert result.result_hash is not None
            known_sources[result.verification_id] = result.result_hash

        evidence_by_id = {item.evidence_id: item for item in self._evidence}
        for item in self._evidence:
            item.verify_hash()
            if item.plan_id != plan.plan_id:
                raise FinalVerificationIntegrityError(
                    "evidence is not bound to the embedded plan"
                )
            if item.step_id is not None and item.step_id not in step_by_id:
                raise FinalVerificationIntegrityError(
                    "evidence references an unknown step"
                )
            expected = known_sources.get(item.source_reference)
            if expected is not None and expected != item.source_hash:
                raise FinalVerificationIntegrityError(
                    "evidence source hash does not match embedded source"
                )
            if expected is None and not item.source_reference.startswith("external:"):
                raise FinalVerificationIntegrityError(
                    "unknown evidence sources must use the external namespace"
                )

        for item in (*self._policy_findings, *self._execution_errors):
            item.verify_hash()
            reference = item.resolution_evidence_id
            if reference is not None:
                evidence = evidence_by_id.get(reference)
                if evidence is None or evidence.status is not FinalEvidenceStatus.VERIFIED:
                    raise FinalVerificationIntegrityError(
                        "resolution evidence is missing or not verified"
                    )
        for item in self._execution_errors:
            if item.step_id is not None and item.step_id not in step_by_id:
                raise FinalVerificationIntegrityError(
                    "execution error references an unknown step"
                )

        memory_by_id: dict[str, ProjectMemoryRecord] = {}
        for record in self._memory_records:
            record.verify_hash()
            if record.project_id != plan.project_id:
                raise FinalVerificationIntegrityError(
                    "project-memory record is not bound to the embedded project"
                )
            current = memory_by_id.get(record.memory_id)
            if current is None or record.revision > current.revision:
                memory_by_id[record.memory_id] = record
            known_sources[f"memory:{record.memory_id}:{record.revision}"] = (
                record.revision_hash
            )

        for link in self._decision_links:
            link.verify_hash()
            record = memory_by_id.get(link.memory_id)
            if record is None or record.kind is not ProjectMemoryKind.DECISION:
                raise FinalVerificationIntegrityError(
                    "decision link references an unknown memory decision"
                )
            if record.revision_hash != link.memory_revision_hash:
                raise FinalVerificationIntegrityError(
                    "decision link revision hash does not match current memory"
                )
            content = record.content
            expected_result_hash = (
                content.get("expected_result_hash")
                if content is not None
                else None
            )
            try:
                normalized_expected = _hash(
                    expected_result_hash,
                    "memory expected_result_hash",
                )
            except FinalVerificationError as exc:
                raise FinalVerificationIntegrityError(
                    "linked memory decision must declare expected_result_hash"
                ) from exc
            if normalized_expected != link.expected_result_hash:
                raise FinalVerificationIntegrityError(
                    "decision link expected result differs from approved memory"
                )
            linked_source_hashes: set[str] = set()
            for evidence_id in link.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or evidence.status is not FinalEvidenceStatus.VERIFIED:
                    raise FinalVerificationIntegrityError(
                        "decision link evidence is missing or not verified"
                    )
                linked_source_hashes.add(evidence.source_hash)
            if link.observed_result_hash not in linked_source_hashes:
                raise FinalVerificationIntegrityError(
                    "decision link observed result lacks matching verified evidence"
                )

        for decision in self._supervision_decisions:
            context = decision.context
            if context.plan_id != plan.plan_id or context.project_id != plan.project_id:
                raise FinalVerificationIntegrityError(
                    "supervision decision is not bound to the embedded execution"
                )
            assert decision.decision_hash is not None
            known_sources[decision.decision_id] = decision.decision_hash

    @property
    def policy(self) -> FinalVerificationPolicy:
        return self._policy

    @property
    def plan(self) -> ExecutionPlan:
        return self._plan

    @property
    def journal(self) -> ExecutionJournal:
        return self._journal

    @property
    def output_validations(self) -> tuple[AgentOutputValidationResult, ...]:
        return self._outputs

    @property
    def payload_verifications(self) -> tuple[ArtifactPayloadVerificationResult, ...]:
        return self._payloads

    @property
    def evidence(self) -> tuple[FinalEvidenceRecord, ...]:
        return self._evidence

    @property
    def policy_findings(self) -> tuple[FinalPolicyFinding, ...]:
        return self._policy_findings

    @property
    def execution_errors(self) -> tuple[FinalExecutionErrorRecord, ...]:
        return self._execution_errors

    @property
    def decision_links(self) -> tuple[FinalDecisionOutcomeLink, ...]:
        return self._decision_links

    @property
    def supervision_decisions(self) -> tuple[MetacognitiveSupervisionDecision, ...]:
        return self._supervision_decisions

    @property
    def memory_records(self) -> tuple[ProjectMemoryRecord, ...]:
        return self._memory_records

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_verification_request",
            "version": self.version,
            "verification_id": self.verification_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "plan_json": self.plan_json,
            "plan_state_hash": self.plan_state_hash,
            "journal_jsonl": self.journal_jsonl,
            "journal_hash": self.journal_hash,
            "journal_head_hash": self.journal_head_hash,
            "journal_event_count": self.journal_event_count,
            "output_validation_jsons": list(self.output_validation_jsons),
            "payload_verification_jsons": list(self.payload_verification_jsons),
            "evidence_jsons": list(self.evidence_jsons),
            "policy_finding_jsons": list(self.policy_finding_jsons),
            "execution_error_jsons": list(self.execution_error_jsons),
            "decision_link_jsons": list(self.decision_link_jsons),
            "supervision_decision_jsons": list(
                self.supervision_decision_jsons
            ),
            "memory_record_jsons": list(self.memory_record_jsons),
            "verifier_id": self.verifier_id,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise FinalVerificationIntegrityError("request hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        verification_id: str,
        policy: FinalVerificationPolicy,
        plan: ExecutionPlan,
        journal: ExecutionJournal,
        output_validations: Iterable[AgentOutputValidationResult],
        payload_verifications: Iterable[ArtifactPayloadVerificationResult] = (),
        evidence: Iterable[FinalEvidenceRecord] = (),
        policy_findings: Iterable[FinalPolicyFinding] = (),
        execution_errors: Iterable[FinalExecutionErrorRecord] = (),
        decision_links: Iterable[FinalDecisionOutcomeLink] = (),
        supervision_decisions: Iterable[
            MetacognitiveSupervisionDecision
        ] = (),
        memory_records: Iterable[ProjectMemoryRecord] = (),
        verifier_id: str,
        requested_at: str | datetime,
    ) -> "FinalVerificationRequest":
        if not isinstance(policy, FinalVerificationPolicy):
            raise FinalVerificationError(
                "policy must be a FinalVerificationPolicy"
            )
        if not isinstance(plan, ExecutionPlan):
            raise FinalVerificationError("plan must be an ExecutionPlan")
        if not isinstance(journal, ExecutionJournal):
            raise FinalVerificationError("journal must be an ExecutionJournal")
        outputs = tuple(output_validations)
        payloads = tuple(payload_verifications)
        if not all(isinstance(item, AgentOutputValidationResult) for item in outputs):
            raise FinalVerificationError(
                "output_validations must contain validation results"
            )
        if not all(
            isinstance(item, ArtifactPayloadVerificationResult)
            for item in payloads
        ):
            raise FinalVerificationError(
                "payload_verifications must contain verification results"
            )
        supplied_evidence = tuple(evidence)
        automatic_evidence = tuple(
            [*(_evidence_for_output(item) for item in outputs),
             *(_evidence_for_payload(item) for item in payloads)]
        )
        combined_evidence = _unique_by(
            (*automatic_evidence, *supplied_evidence),
            "evidence_id",
            "evidence records",
        )
        seal = journal.seal()
        return cls(
            verification_id=verification_id,
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            plan_json=plan.to_json(),
            plan_state_hash=_plan_state_hash(plan),
            journal_jsonl=journal.to_jsonl(),
            journal_hash=seal.journal_hash,
            journal_head_hash=seal.head_hash,
            journal_event_count=seal.event_count,
            output_validation_jsons=tuple(item.to_json() for item in outputs),
            payload_verification_jsons=tuple(item.to_json() for item in payloads),
            evidence_jsons=tuple(item.to_json() for item in combined_evidence),
            policy_finding_jsons=tuple(item.to_json() for item in policy_findings),
            execution_error_jsons=tuple(item.to_json() for item in execution_errors),
            decision_link_jsons=tuple(item.to_json() for item in decision_links),
            supervision_decision_jsons=tuple(
                item.to_json() for item in supervision_decisions
            ),
            memory_record_jsons=tuple(item.to_json() for item in memory_records),
            verifier_id=verifier_id,
            requested_at=_timestamp(requested_at, "requested_at"),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalVerificationRequest":
        if data.get("record_type") != "final_verification_request":
            raise FinalVerificationError(
                "record_type must be final_verification_request"
            )
        if "request_hash" not in data:
            raise FinalVerificationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            verification_id=data["verification_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            plan_json=data["plan_json"],
            plan_state_hash=data["plan_state_hash"],
            journal_jsonl=data["journal_jsonl"],
            journal_hash=data["journal_hash"],
            journal_head_hash=data["journal_head_hash"],
            journal_event_count=data["journal_event_count"],
            output_validation_jsons=tuple(data["output_validation_jsons"]),
            payload_verification_jsons=tuple(data["payload_verification_jsons"]),
            evidence_jsons=tuple(data["evidence_jsons"]),
            policy_finding_jsons=tuple(data["policy_finding_jsons"]),
            execution_error_jsons=tuple(data["execution_error_jsons"]),
            decision_link_jsons=tuple(data["decision_link_jsons"]),
            supervision_decision_jsons=tuple(data["supervision_decision_jsons"]),
            memory_record_jsons=tuple(data["memory_record_jsons"]),
            verifier_id=data["verifier_id"],
            requested_at=data["requested_at"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalVerificationRequest":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "request_json")))


@dataclass(frozen=True, slots=True)
class FinalVerificationGateResult:
    gate: FinalVerificationGate
    passed: bool
    checked_count: int
    issue_codes: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            gate = FinalVerificationGate(self.gate)
        except (TypeError, ValueError) as exc:
            raise FinalVerificationError("verification gate is invalid") from exc
        object.__setattr__(self, "gate", gate)
        object.__setattr__(self, "passed", _boolean(self.passed, "passed"))
        object.__setattr__(
            self,
            "checked_count",
            _non_negative_int(self.checked_count, "checked_count"),
        )
        codes = _identifiers(self.issue_codes, "issue_code", _TOKEN)
        references = _identifiers(self.references, "reference")
        if self.passed == bool(codes):
            raise FinalVerificationIntegrityError(
                "passed gates cannot have issues and failed gates must have issues"
            )
        object.__setattr__(self, "issue_codes", codes)
        object.__setattr__(self, "references", references)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "passed": self.passed,
            "checked_count": self.checked_count,
            "issue_codes": list(self.issue_codes),
            "references": list(self.references),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalVerificationGateResult":
        return cls(
            gate=data["gate"],
            passed=data["passed"],
            checked_count=data["checked_count"],
            issue_codes=tuple(data.get("issue_codes", ())),
            references=tuple(data.get("references", ())),
        )


@dataclass(frozen=True, slots=True)
class FinalReportSigner:
    """HMAC signer that never includes secret bytes in its representation."""

    key_id: str
    secret: bytes = field(repr=False)
    algorithm: str = FINAL_REPORT_SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _identifier(self.key_id, "key_id"))
        if not isinstance(self.secret, bytes):
            raise FinalReportSignatureError("signing secret must be bytes")
        if len(self.secret) < FINAL_REPORT_MINIMUM_KEY_BYTES:
            raise FinalReportSignatureError(
                f"signing secret must contain at least {FINAL_REPORT_MINIMUM_KEY_BYTES} bytes"
            )
        if self.algorithm != FINAL_REPORT_SIGNATURE_ALGORITHM:
            raise FinalReportSignatureError("unsupported final report algorithm")

    def sign_hash(self, report_hash: str) -> str:
        digest = _hash(report_hash, "report_hash")
        return hmac.new(self.secret, digest.encode("ascii"), hashlib.sha256).hexdigest()

    def verify(self, report: "FinalVerificationReport") -> bool:
        if not isinstance(report, FinalVerificationReport):
            return False
        if report.key_id != self.key_id or report.algorithm != self.algorithm:
            return False
        expected = self.sign_hash(report.report_hash)
        return hmac.compare_digest(expected, report.signature)


@dataclass(frozen=True, slots=True)
class FinalVerificationReport:
    report_id: str
    verification_id: str
    request_hash: str
    policy_id: str
    policy_hash: str
    plan_id: str
    project_id: str
    plan_state_hash: str
    journal_hash: str
    status: FinalVerificationStatus
    gates: tuple[FinalVerificationGateResult, ...]
    verified_by: str
    verified_at: str
    key_id: str
    algorithm: str
    report_hash: str
    signature: str
    version: int = FINAL_VERIFICATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "report_id",
            "verification_id",
            "policy_id",
            "plan_id",
            "project_id",
            "key_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in (
            "request_hash",
            "policy_hash",
            "plan_state_hash",
            "journal_hash",
            "report_hash",
            "signature",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        try:
            status = FinalVerificationStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise FinalVerificationError("final report status is invalid") from exc
        object.__setattr__(self, "status", status)
        gates = tuple(sorted(self.gates, key=lambda item: item.gate.value))
        if not all(isinstance(item, FinalVerificationGateResult) for item in gates):
            raise FinalVerificationError("gates must contain gate results")
        if {item.gate for item in gates} != set(FinalVerificationGate):
            raise FinalVerificationIntegrityError(
                "final report must contain every verification gate exactly once"
            )
        expected_status = (
            FinalVerificationStatus.VERIFIED
            if all(item.passed for item in gates)
            else FinalVerificationStatus.REJECTED
        )
        if status is not expected_status:
            raise FinalVerificationIntegrityError(
                "report status does not match verification gates"
            )
        object.__setattr__(self, "gates", gates)
        object.__setattr__(
            self, "verified_by", _identifier(self.verified_by, "verified_by", _AGENT_ID)
        )
        object.__setattr__(self, "verified_at", _timestamp(self.verified_at, "verified_at"))
        if self.algorithm != FINAL_REPORT_SIGNATURE_ALGORITHM:
            raise FinalReportSignatureError("unsupported report signature algorithm")
        if self.version != FINAL_VERIFICATION_FORMAT_VERSION:
            raise FinalVerificationError("unsupported final report version")
        if self.compute_hash() != self.report_hash:
            raise FinalVerificationIntegrityError(
                "report_hash does not match final report content"
            )
        expected_id = f"final-report:{self.report_hash}"
        if self.report_id != expected_id:
            raise FinalVerificationIntegrityError(
                "report_id does not match report_hash"
            )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "final_verification_report",
            "version": self.version,
            "verification_id": self.verification_id,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "plan_state_hash": self.plan_state_hash,
            "journal_hash": self.journal_hash,
            "status": self.status.value,
            "gates": [item.to_dict() for item in self.gates],
            "verified_by": self.verified_by,
            "verified_at": self.verified_at,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.report_hash != self.compute_hash():
            raise FinalVerificationIntegrityError("final report hash mismatch")

    def verify_signature(self, signer: FinalReportSigner) -> None:
        self.verify_hash()
        if not isinstance(signer, FinalReportSigner) or not signer.verify(self):
            raise FinalReportSignatureError("final report signature is invalid")

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data.update(
            {
                "report_id": self.report_id,
                "report_hash": self.report_hash,
                "signature": self.signature,
            }
        )
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FinalVerificationReport":
        if data.get("record_type") != "final_verification_report":
            raise FinalVerificationError(
                "record_type must be final_verification_report"
            )
        for name in ("report_id", "report_hash", "signature"):
            if name not in data:
                raise FinalReportSignatureError(
                    f"serialized final report is missing {name}"
                )
        return cls(
            report_id=data["report_id"],
            verification_id=data["verification_id"],
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            plan_state_hash=data["plan_state_hash"],
            journal_hash=data["journal_hash"],
            status=data["status"],
            gates=tuple(FinalVerificationGateResult.from_dict(item) for item in data["gates"]),
            verified_by=data["verified_by"],
            verified_at=data["verified_at"],
            key_id=data["key_id"],
            algorithm=data["algorithm"],
            report_hash=data["report_hash"],
            signature=data["signature"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(cls, payload: str) -> "FinalVerificationReport":
        return cls.from_dict(json.loads(_canonical_json_object(payload, "report_json")))


def _gate(
    gate: FinalVerificationGate,
    issues: Iterable[str],
    checked_count: int,
    references: Iterable[str] = (),
) -> FinalVerificationGateResult:
    normalized_issues = _identifiers(issues, "issue_code", _TOKEN)
    return FinalVerificationGateResult(
        gate=gate,
        passed=not normalized_issues,
        checked_count=checked_count,
        issue_codes=normalized_issues,
        references=_identifiers(references, "reference"),
    )


@dataclass(frozen=True, slots=True)
class FinalVerifier:
    request: FinalVerificationRequest
    signer: FinalReportSigner

    def __post_init__(self) -> None:
        if not isinstance(self.request, FinalVerificationRequest):
            raise FinalVerificationError(
                "request must be a FinalVerificationRequest"
            )
        if not isinstance(self.signer, FinalReportSigner):
            raise FinalReportSignatureError("signer must be a FinalReportSigner")
        self.request.verify_hash()

    def verify(
        self,
        *,
        verified_at: str | datetime | None = None,
    ) -> FinalVerificationReport:
        request = self.request
        gates = (
            self._verify_plan(),
            self._verify_journal(),
            self._verify_outputs(),
            self._verify_artifacts(),
            self._verify_evidence(),
            self._verify_policy_findings(),
            self._verify_errors(),
            self._verify_decisions(),
            self._verify_supervision(),
        )
        status = (
            FinalVerificationStatus.VERIFIED
            if all(item.passed for item in gates)
            else FinalVerificationStatus.REJECTED
        )
        effective_time = _timestamp(
            verified_at if verified_at is not None else request.requested_at,
            "verified_at",
        )
        material = {
            "record_type": "final_verification_report",
            "version": FINAL_VERIFICATION_FORMAT_VERSION,
            "verification_id": request.verification_id,
            "request_hash": request.request_hash,
            "policy_id": request.policy.policy_id,
            "policy_hash": request.policy.policy_hash,
            "plan_id": request.plan.plan_id,
            "project_id": request.plan.project_id,
            "plan_state_hash": request.plan_state_hash,
            "journal_hash": request.journal_hash,
            "status": status.value,
            "gates": [item.to_dict() for item in sorted(gates, key=lambda item: item.gate.value)],
            "verified_by": request.verifier_id,
            "verified_at": effective_time,
            "key_id": self.signer.key_id,
            "algorithm": self.signer.algorithm,
        }
        report_hash = _sha256_document(material)
        report = FinalVerificationReport(
            report_id=f"final-report:{report_hash}",
            verification_id=request.verification_id,
            request_hash=request.request_hash or "",
            policy_id=request.policy.policy_id,
            policy_hash=request.policy.policy_hash,
            plan_id=request.plan.plan_id,
            project_id=request.plan.project_id,
            plan_state_hash=request.plan_state_hash,
            journal_hash=request.journal_hash,
            status=status,
            gates=gates,
            verified_by=request.verifier_id,
            verified_at=effective_time,
            key_id=self.signer.key_id,
            algorithm=self.signer.algorithm,
            report_hash=report_hash,
            signature=self.signer.sign_hash(report_hash),
        )
        report.verify_signature(self.signer)
        return report

    def _verify_plan(self) -> FinalVerificationGateResult:
        plan = self.request.plan
        issues: list[str] = []
        if plan.status is not PlanStatus.COMPLETED:
            issues.append("plan.not-completed")
        for step in plan.steps:
            if step.status is not StepStatus.COMPLETED:
                issues.append("plan.step-incomplete")
            if step.assigned_agent_id is None:
                issues.append("plan.agent-unassigned")
            if step.requires_human_approval and step.approval_reference is None:
                issues.append("plan.approval-missing")
        if plan.requires_human_approval and plan.approval_reference is None:
            issues.append("plan.approval-missing")
        return _gate(
            FinalVerificationGate.PLAN_COMPLETION,
            issues,
            len(plan.steps),
            (step.step_id for step in plan.steps),
        )

    def _verify_journal(self) -> FinalVerificationGateResult:
        journal = self.request.journal
        issues: list[str] = []
        events = journal.events
        if not events or events[-1].event_type is not ExecutionEventType.PLAN_COMPLETED:
            issues.append("journal.terminal-completion-missing")
        completed_steps = {
            event.step_id
            for event in events
            if event.event_type is ExecutionEventType.STEP_COMPLETED
        }
        for step in self.request.plan.steps:
            if step.step_id not in completed_steps:
                issues.append("journal.step-completion-missing")
        failed_events = tuple(
            event
            for event in events
            if event.event_type
            in {
                ExecutionEventType.PLAN_FAILED,
                ExecutionEventType.STEP_FAILED,
                ExecutionEventType.PLAN_BLOCKED,
                ExecutionEventType.STEP_BLOCKED,
            }
        )
        if failed_events and not self.request.execution_errors:
            issues.append("journal.failure-unaccounted")
        return _gate(
            FinalVerificationGate.JOURNAL_INTEGRITY,
            issues,
            len(events),
            (f"journal:{journal.plan_id}",),
        )

    def _verify_outputs(self) -> FinalVerificationGateResult:
        by_step: dict[str, list[AgentOutputValidationResult]] = defaultdict(list)
        issues: list[str] = []
        for result in self.request.output_validations:
            by_step[result.step_id].append(result)
            if result.status is AgentOutputValidationStatus.REJECTED:
                issues.append("output.rejected")
            elif result.status is AgentOutputValidationStatus.REQUIRES_REVIEW:
                issues.append("output.review-required")
            if result.rejected_count:
                issues.append("output.rejected-records")
            if result.review_count:
                issues.append("output.review-records")
        for step in self.request.plan.steps:
            count = len(by_step.get(step.step_id, ()))
            if count == 0:
                issues.append("output.missing")
            elif count > 1:
                issues.append("output.duplicate")
        return _gate(
            FinalVerificationGate.OUTPUT_VALIDATION,
            issues,
            len(self.request.output_validations),
            (item.validation_id for item in self.request.output_validations),
        )

    def _verify_artifacts(self) -> FinalVerificationGateResult:
        by_source: dict[str, list[ArtifactPayloadVerificationResult]] = defaultdict(list)
        issues: list[str] = []
        for result in self.request.payload_verifications:
            by_source[result.validation_result_hash].append(result)
            if result.status is ArtifactPayloadVerificationStatus.REJECTED:
                issues.append("artifact.rejected")
            elif result.status is ArtifactPayloadVerificationStatus.REQUIRES_REVIEW:
                issues.append("artifact.review-required")
            if result.rejected_count:
                issues.append("artifact.rejected-records")
            if result.review_count:
                issues.append("artifact.review-records")
        for output in self.request.output_validations:
            assert output.result_hash is not None
            matches = by_source.get(output.result_hash, ())
            if output.accepted_count > 0 and not matches:
                issues.append("artifact.verification-missing")
            if len(matches) > 1:
                issues.append("artifact.verification-duplicate")
            if matches and matches[0].verified_count != output.accepted_count:
                issues.append("artifact.count-mismatch")
        return _gate(
            FinalVerificationGate.ARTIFACT_INTEGRITY,
            issues,
            len(self.request.payload_verifications),
            (item.verification_id for item in self.request.payload_verifications),
        )

    def _verify_evidence(self) -> FinalVerificationGateResult:
        verified_by_step: Counter[str] = Counter()
        issues: list[str] = []
        for item in self.request.evidence:
            if item.status is FinalEvidenceStatus.VERIFIED and item.step_id is not None:
                verified_by_step[item.step_id] += 1
            elif item.status is FinalEvidenceStatus.FAILED:
                issues.append("evidence.failed")
            elif item.status is FinalEvidenceStatus.REQUIRES_REVIEW:
                issues.append("evidence.review-required")
        minimum = self.request.policy.minimum_verified_evidence_per_step
        for step in self.request.plan.steps:
            if verified_by_step[step.step_id] < minimum:
                issues.append("evidence.step-insufficient")
        return _gate(
            FinalVerificationGate.EVIDENCE_COMPLETENESS,
            issues,
            len(self.request.evidence),
            (item.evidence_id for item in self.request.evidence),
        )

    def _verify_policy_findings(self) -> FinalVerificationGateResult:
        unresolved = [item for item in self.request.policy_findings if not item.resolved]
        issues = ("policy.unresolved-violation",) if unresolved else ()
        return _gate(
            FinalVerificationGate.POLICY_COMPLIANCE,
            issues,
            len(self.request.policy_findings),
            (item.finding_id for item in self.request.policy_findings),
        )

    def _verify_errors(self) -> FinalVerificationGateResult:
        unresolved = [item for item in self.request.execution_errors if not item.resolved]
        issues = ("error.unresolved",) if unresolved else ()
        return _gate(
            FinalVerificationGate.ERROR_RESOLUTION,
            issues,
            len(self.request.execution_errors),
            (item.error_id for item in self.request.execution_errors),
        )

    def _verify_decisions(self) -> FinalVerificationGateResult:
        latest: dict[str, ProjectMemoryRecord] = {}
        for record in self.request.memory_records:
            current = latest.get(record.memory_id)
            if current is None or record.revision > current.revision:
                latest[record.memory_id] = record
        active = {
            memory_id: record
            for memory_id, record in latest.items()
            if record.kind is ProjectMemoryKind.DECISION
            and record.state is ProjectMemoryState.ACTIVE
        }
        links_by_memory: dict[str, list[FinalDecisionOutcomeLink]] = defaultdict(list)
        for link in self.request.decision_links:
            links_by_memory[link.memory_id].append(link)
        issues: list[str] = []
        for memory_id in active:
            links = links_by_memory.get(memory_id, ())
            if not links:
                issues.append("decision.link-missing")
            elif len(links) > 1:
                issues.append("decision.link-duplicate")
            elif not links[0].coherent:
                issues.append("decision.outcome-mismatch")
        for memory_id in links_by_memory:
            if memory_id not in active:
                issues.append("decision.link-not-active")
        return _gate(
            FinalVerificationGate.DECISION_COHERENCE,
            issues,
            len(active),
            tuple(sorted(active)),
        )

    def _verify_supervision(self) -> FinalVerificationGateResult:
        current = [
            decision
            for decision in self.request.supervision_decisions
            if decision.context.plan_state_hash == self.request.plan_state_hash
            and decision.context.journal_hash == self.request.journal_hash
        ]
        issues: list[str] = []
        references: tuple[str, ...] = ()
        if not current:
            issues.append("supervision.current-decision-missing")
        else:
            latest = max(current, key=lambda item: (item.decided_at, item.decision_id))
            references = (latest.decision_id,)
            if latest.action is not MetacognitiveDecisionAction.CONTINUE:
                issues.append("supervision.clearance-denied")
            if latest.approval_required and latest.approval_reference is None:
                issues.append("supervision.approval-missing")
        return _gate(
            FinalVerificationGate.SUPERVISION_CLEARANCE,
            issues,
            len(current),
            references,
        )
