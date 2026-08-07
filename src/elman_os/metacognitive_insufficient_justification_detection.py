"""Deterministic insufficient-justification detection for ELMAN-OS v0.7.

The detector evaluates an existing, hash-bound metacognitive supervision
decision against an explicit justification policy. It does not infer semantic
meaning from natural language. Instead, it checks deterministic, auditable
signals such as rationale length and exact citations of evidence, findings,
corrective steps, and required approval references.

The boundary is read-only: it does not mutate decisions, plans, orchestration
state, policies, journals, or memory; it does not dispatch agents, invoke AI
providers, persist state, or perform network access.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .agent_contracts import canonical_json
from .metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecision,
    MetacognitiveSupervisionFinding,
)

METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetacognitiveInsufficientJustificationDetectionError(ValueError):
    """A justification-detection contract or operation is invalid."""


class MetacognitiveInsufficientJustificationDetectionIntegrityError(
    MetacognitiveInsufficientJustificationDetectionError
):
    """A serialized contract or binding fails deterministic integrity checks."""


class MetacognitiveInsufficientJustificationDetectionPolicyError(
    MetacognitiveInsufficientJustificationDetectionError
):
    """A configured justification policy is unsafe or inconsistent."""


class MetacognitiveInsufficientJustificationDetectionStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"


class MetacognitiveJustificationGapKind(StrEnum):
    RATIONALE_TOO_SHORT = "rationale-too-short"
    INSUFFICIENT_EVIDENCE_CITATIONS = "insufficient-evidence-citations"
    MISSING_FINDING_CITATION = "missing-finding-citation"
    MISSING_CORRECTIVE_STEP_CITATION = "missing-corrective-step-citation"
    MISSING_APPROVAL_REFERENCE = "missing-approval-reference"
    MISSING_APPROVAL_REFERENCE_CITATION = "missing-approval-reference-citation"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveInsufficientJustificationDetectionError(
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
        raise MetacognitiveInsufficientJustificationDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveInsufficientJustificationDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveInsufficientJustificationDetectionError(
            f"{name} must be a boolean"
        )
    return value


def _integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise MetacognitiveInsufficientJustificationDetectionError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveInsufficientJustificationDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveInsufficientJustificationDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveInsufficientJustificationDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveInsufficientJustificationDetectionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveInsufficientJustificationDetectionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveInsufficientJustificationDetectionPolicy:
    """Explicit deterministic criteria for decision justification."""

    policy_id: str
    minimum_rationale_characters: int = 48
    minimum_cited_evidence_references: int = 1
    require_all_finding_citations: bool = True
    require_corrective_step_citations: bool = True
    require_approval_reference_when_required: bool = True
    require_approval_reference_citation: bool = True
    finding_confidence_bp: int = 9500
    fail_closed: bool = True
    version: int = (
        METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "minimum_rationale_characters",
            _integer(
                self.minimum_rationale_characters,
                "minimum_rationale_characters",
                minimum=1,
                maximum=100_000,
            ),
        )
        object.__setattr__(
            self,
            "minimum_cited_evidence_references",
            _integer(
                self.minimum_cited_evidence_references,
                "minimum_cited_evidence_references",
                minimum=0,
                maximum=10_000,
            ),
        )
        for name in (
            "require_all_finding_citations",
            "require_corrective_step_citations",
            "require_approval_reference_when_required",
            "require_approval_reference_citation",
            "fail_closed",
        ):
            object.__setattr__(
                self,
                name,
                _boolean(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "finding_confidence_bp",
            _integer(
                self.finding_confidence_bp,
                "finding_confidence_bp",
                minimum=0,
                maximum=10_000,
            ),
        )
        if not self.fail_closed:
            raise MetacognitiveInsufficientJustificationDetectionPolicyError(
                "insufficient-justification detection must fail closed"
            )
        if (
            self.require_approval_reference_citation
            and not self.require_approval_reference_when_required
        ):
            raise MetacognitiveInsufficientJustificationDetectionPolicyError(
                "approval-reference citation requires approval-reference enforcement"
            )
        if self.version != (
            METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "unsupported insufficient-justification policy format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": (
                "metacognitive_insufficient_justification_detection_policy"
            ),
            "version": self.version,
            "policy_id": self.policy_id,
            "minimum_rationale_characters": self.minimum_rationale_characters,
            "minimum_cited_evidence_references": (
                self.minimum_cited_evidence_references
            ),
            "require_all_finding_citations": self.require_all_finding_citations,
            "require_corrective_step_citations": (
                self.require_corrective_step_citations
            ),
            "require_approval_reference_when_required": (
                self.require_approval_reference_when_required
            ),
            "require_approval_reference_citation": (
                self.require_approval_reference_citation
            ),
            "finding_confidence_bp": self.finding_confidence_bp,
            "fail_closed": self.fail_closed,
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
    ) -> "MetacognitiveInsufficientJustificationDetectionPolicy":
        if data.get("record_type") != (
            "metacognitive_insufficient_justification_detection_policy"
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "record_type must be "
                "metacognitive_insufficient_justification_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            minimum_rationale_characters=data["minimum_rationale_characters"],
            minimum_cited_evidence_references=data[
                "minimum_cited_evidence_references"
            ],
            require_all_finding_citations=data[
                "require_all_finding_citations"
            ],
            require_corrective_step_citations=data[
                "require_corrective_step_citations"
            ],
            require_approval_reference_when_required=data[
                "require_approval_reference_when_required"
            ],
            require_approval_reference_citation=data[
                "require_approval_reference_citation"
            ],
            finding_confidence_bp=data["finding_confidence_bp"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveInsufficientJustificationDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveInsufficientJustificationDetectionRequest:
    """Hash-bound request tying a policy to one supervision decision."""

    request_id: str
    policy_json: str
    policy_hash: str
    decision_json: str
    decision_hash: str
    context_hash: str
    requested_by: str
    requested_at: str
    reason: str
    request_hash: str | None = None
    version: int = (
        METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )
        policy = MetacognitiveInsufficientJustificationDetectionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        decision = MetacognitiveSupervisionDecision.from_json(
            _text(self.decision_json, "decision_json")
        )
        decision.verify_hash()
        supplied_decision_hash = _hash(self.decision_hash, "decision_hash")
        if supplied_decision_hash != decision.decision_hash:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "decision_hash does not match embedded decision"
            )
        object.__setattr__(self, "decision_json", decision.to_json())
        object.__setattr__(self, "decision_hash", supplied_decision_hash)

        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != decision.context_hash:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "context_hash does not match embedded decision context"
            )
        object.__setattr__(self, "context_hash", supplied_context_hash)

        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < decision.decided_at:
            raise MetacognitiveInsufficientJustificationDetectionPolicyError(
                "justification detection cannot precede the decision"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

        if self.version != (
            METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "unsupported insufficient-justification request format version"
            )
        expected_id = (
            f"insufficient-justification-request:{self.compute_identity_hash()}"
        )
        if self.request_id != expected_id:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveInsufficientJustificationDetectionPolicy:
        return MetacognitiveInsufficientJustificationDetectionPolicy.from_json(
            self.policy_json
        )

    @property
    def decision(self) -> MetacognitiveSupervisionDecision:
        return MetacognitiveSupervisionDecision.from_json(self.decision_json)

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return self.decision.context

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "decision_hash": self.decision_hash,
            "context_hash": self.context_hash,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "metacognitive_insufficient_justification_detection_request"
            ),
            "version": self.version,
            "request_id": self.request_id,
            "policy_json": self.policy_json,
            "decision_json": self.decision_json,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "request_hash does not match request content"
            )

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
        policy: MetacognitiveInsufficientJustificationDetectionPolicy,
        decision: MetacognitiveSupervisionDecision,
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveInsufficientJustificationDetectionRequest":
        if not isinstance(
            policy,
            MetacognitiveInsufficientJustificationDetectionPolicy,
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "policy must be an insufficient-justification detection policy"
            )
        if not isinstance(decision, MetacognitiveSupervisionDecision):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "decision must be a MetacognitiveSupervisionDecision"
            )
        decision.verify_hash()
        normalized_requested_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_requested_at = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        normalized_reason = _text(reason, "reason")
        identity = {
            "policy_hash": policy.policy_hash,
            "decision_hash": decision.decision_hash,
            "context_hash": decision.context_hash,
            "requested_by": normalized_requested_by,
            "requested_at": normalized_requested_at,
            "reason": normalized_reason,
        }
        return cls(
            request_id=(
                "insufficient-justification-request:"
                f"{_sha256_document(identity)}"
            ),
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            decision_json=decision.to_json(),
            decision_hash=decision.decision_hash or "",
            context_hash=decision.context_hash,
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveInsufficientJustificationDetectionRequest":
        if data.get("record_type") != (
            "metacognitive_insufficient_justification_detection_request"
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "record_type must be "
                "metacognitive_insufficient_justification_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            request_id=data["request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            decision_json=data["decision_json"],
            decision_hash=data["decision_hash"],
            context_hash=data["context_hash"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            reason=data["reason"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveInsufficientJustificationDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveJustificationGap:
    """One deterministic justification deficiency."""

    gap_id: str
    kind: MetacognitiveJustificationGapKind
    decision_id: str
    decision_hash: str
    evidence_reference: str
    detail: str
    affected_step_ids: tuple[str, ...] = ()
    gap_hash: str | None = None
    version: int = (
        METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gap_id",
            _identifier(self.gap_id, "gap_id"),
        )
        try:
            kind = MetacognitiveJustificationGapKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                "justification gap kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "decision_id",
            _identifier(self.decision_id, "decision_id"),
        )
        object.__setattr__(
            self,
            "decision_hash",
            _hash(self.decision_hash, "decision_hash"),
        )
        object.__setattr__(
            self,
            "evidence_reference",
            _identifier(self.evidence_reference, "evidence_reference"),
        )
        object.__setattr__(self, "detail", _text(self.detail, "detail"))
        if isinstance(self.affected_step_ids, (str, bytes)):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "affected_step_ids must be an iterable"
            )
        normalized_steps = tuple(
            sorted(
                {
                    _identifier(value, "affected_step_id", _STEP_ID)
                    for value in self.affected_step_ids
                }
            )
        )
        object.__setattr__(self, "affected_step_ids", normalized_steps)
        if self.version != (
            METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "unsupported justification gap format version"
            )
        expected_id = f"justification-gap:{self.compute_identity_hash()}"
        if self.gap_id != expected_id:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "gap_id does not match gap identity"
            )
        computed = self.compute_hash()
        if self.gap_hash is None:
            object.__setattr__(self, "gap_hash", computed)
        else:
            supplied = _hash(self.gap_hash, "gap_hash")
            if supplied != computed:
                raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                    "gap_hash does not match gap content"
                )
            object.__setattr__(self, "gap_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "decision_id": self.decision_id,
            "decision_hash": self.decision_hash,
            "evidence_reference": self.evidence_reference,
            "detail": self.detail,
            "affected_step_ids": list(self.affected_step_ids),
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_justification_gap",
            "version": self.version,
            "gap_id": self.gap_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.gap_hash != self.compute_hash():
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "gap_hash does not match gap content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["gap_hash"] = self.gap_hash
        return data

    @classmethod
    def capture(
        cls,
        *,
        kind: MetacognitiveJustificationGapKind | str,
        decision: MetacognitiveSupervisionDecision,
        evidence_reference: str,
        detail: str,
        affected_step_ids: Iterable[str] = (),
    ) -> "MetacognitiveJustificationGap":
        if not isinstance(decision, MetacognitiveSupervisionDecision):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "decision must be a MetacognitiveSupervisionDecision"
            )
        decision.verify_hash()
        normalized_kind = MetacognitiveJustificationGapKind(kind)
        normalized_evidence = _identifier(
            evidence_reference,
            "evidence_reference",
        )
        if normalized_evidence not in decision.context.evidence_references:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "gap evidence is not bound to the decision context"
            )
        if isinstance(affected_step_ids, (str, bytes)):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "affected_step_ids must be an iterable"
            )
        normalized_steps = tuple(
            sorted(
                {
                    _identifier(value, "affected_step_id", _STEP_ID)
                    for value in affected_step_ids
                }
            )
        )
        normalized_detail = _text(detail, "detail")
        identity = {
            "kind": normalized_kind.value,
            "decision_id": decision.decision_id,
            "decision_hash": decision.decision_hash,
            "evidence_reference": normalized_evidence,
            "detail": normalized_detail,
            "affected_step_ids": list(normalized_steps),
        }
        return cls(
            gap_id=f"justification-gap:{_sha256_document(identity)}",
            kind=normalized_kind,
            decision_id=decision.decision_id,
            decision_hash=decision.decision_hash or "",
            evidence_reference=normalized_evidence,
            detail=normalized_detail,
            affected_step_ids=normalized_steps,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveJustificationGap":
        if data.get("record_type") != "metacognitive_justification_gap":
            raise MetacognitiveInsufficientJustificationDetectionError(
                "record_type must be metacognitive_justification_gap"
            )
        if "gap_hash" not in data:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "serialized gap is missing gap_hash"
            )
        return cls(
            gap_id=data["gap_id"],
            kind=data["kind"],
            decision_id=data["decision_id"],
            decision_hash=data["decision_hash"],
            evidence_reference=data["evidence_reference"],
            detail=data["detail"],
            affected_step_ids=tuple(data["affected_step_ids"]),
            gap_hash=data["gap_hash"],
            version=data.get("version", 0),
        )


@dataclass(frozen=True, slots=True)
class MetacognitiveInsufficientJustificationDetectionResult:
    """Immutable result containing deterministic gaps and supervision findings."""

    result_id: str
    request_id: str
    request_hash: str
    status: MetacognitiveInsufficientJustificationDetectionStatus
    gaps: tuple[MetacognitiveJustificationGap, ...]
    findings: tuple[MetacognitiveSupervisionFinding, ...]
    result_hash: str | None = None
    version: int = (
        METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "result_id",
            _identifier(self.result_id, "result_id"),
        )
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "request_hash",
            _hash(self.request_hash, "request_hash"),
        )
        try:
            status = MetacognitiveInsufficientJustificationDetectionStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        gaps = tuple(sorted(self.gaps, key=lambda item: item.gap_id))
        findings = tuple(
            sorted(self.findings, key=lambda item: item.finding_id)
        )
        if not all(isinstance(item, MetacognitiveJustificationGap) for item in gaps):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "gaps must contain MetacognitiveJustificationGap values"
            )
        if not all(
            isinstance(item, MetacognitiveSupervisionFinding)
            for item in findings
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "findings must contain MetacognitiveSupervisionFinding values"
            )
        for gap in gaps:
            gap.verify_hash()
        for finding in findings:
            finding.verify_hash()
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "findings", findings)

        expected_status = (
            MetacognitiveInsufficientJustificationDetectionStatus.INSUFFICIENT
            if gaps
            else MetacognitiveInsufficientJustificationDetectionStatus.SUFFICIENT
        )
        if status is not expected_status:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "result status does not match detected justification gaps"
            )
        if len(gaps) != len(findings):
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "result must contain exactly one finding per justification gap"
            )
        if self.version != (
            METACOGNITIVE_INSUFFICIENT_JUSTIFICATION_DETECTION_FORMAT_VERSION
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "unsupported insufficient-justification result format version"
            )
        expected_id = (
            f"insufficient-justification-result:{self.compute_identity_hash()}"
        )
        if self.result_id != expected_id:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "result_id does not match result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                    "result_hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "gap_hashes": [item.gap_hash for item in self.gaps],
            "finding_hashes": [item.finding_hash for item in self.findings],
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "metacognitive_insufficient_justification_detection_result"
            ),
            "version": self.version,
            "result_id": self.result_id,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "gaps": [item.to_dict() for item in self.gaps],
            "findings": [item.to_dict() for item in self.findings],
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "result_hash does not match result content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["result_hash"] = self.result_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        request: MetacognitiveInsufficientJustificationDetectionRequest,
        gaps: Iterable[MetacognitiveJustificationGap],
        findings: Iterable[MetacognitiveSupervisionFinding],
    ) -> "MetacognitiveInsufficientJustificationDetectionResult":
        if not isinstance(
            request,
            MetacognitiveInsufficientJustificationDetectionRequest,
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "request must be an insufficient-justification detection request"
            )
        request.verify_hash()
        normalized_gaps = tuple(sorted(gaps, key=lambda item: item.gap_id))
        normalized_findings = tuple(
            sorted(findings, key=lambda item: item.finding_id)
        )
        status = (
            MetacognitiveInsufficientJustificationDetectionStatus.INSUFFICIENT
            if normalized_gaps
            else MetacognitiveInsufficientJustificationDetectionStatus.SUFFICIENT
        )
        identity = {
            "request_id": request.request_id,
            "request_hash": request.request_hash,
            "status": status.value,
            "gap_hashes": [item.gap_hash for item in normalized_gaps],
            "finding_hashes": [
                item.finding_hash for item in normalized_findings
            ],
        }
        return cls(
            result_id=(
                "insufficient-justification-result:"
                f"{_sha256_document(identity)}"
            ),
            request_id=request.request_id,
            request_hash=request.request_hash or "",
            status=status,
            gaps=normalized_gaps,
            findings=normalized_findings,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveInsufficientJustificationDetectionResult":
        if data.get("record_type") != (
            "metacognitive_insufficient_justification_detection_result"
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "record_type must be "
                "metacognitive_insufficient_justification_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveInsufficientJustificationDetectionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            result_id=data["result_id"],
            request_id=data["request_id"],
            request_hash=data["request_hash"],
            status=data["status"],
            gaps=tuple(
                MetacognitiveJustificationGap.from_dict(item)
                for item in data["gaps"]
            ),
            findings=tuple(
                MetacognitiveSupervisionFinding.from_dict(item)
                for item in data["findings"]
            ),
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveInsufficientJustificationDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "insufficient-justification result JSON must be an object"
            )
        return cls.from_dict(data)


def _contains_exact(text: str, value: str) -> bool:
    """Return True when the exact auditable identifier occurs in the rationale."""

    return value in text


class MetacognitiveInsufficientJustificationDetector:
    """Read-only deterministic detector for insufficient decision justification."""

    def detect(
        self,
        request: MetacognitiveInsufficientJustificationDetectionRequest,
    ) -> MetacognitiveInsufficientJustificationDetectionResult:
        if not isinstance(
            request,
            MetacognitiveInsufficientJustificationDetectionRequest,
        ):
            raise MetacognitiveInsufficientJustificationDetectionError(
                "request must be an insufficient-justification detection request"
            )
        request.verify_hash()
        policy = request.policy
        decision = request.decision
        decision.verify_hash()
        context = decision.context
        context.verify_hash()

        evidence_reference = context.evidence_references[0]
        rationale = decision.rationale
        gaps: list[MetacognitiveJustificationGap] = []
        findings: list[MetacognitiveSupervisionFinding] = []

        def add_gap(
            *,
            kind: MetacognitiveJustificationGapKind,
            detail: str,
            affected_step_ids: Iterable[str] = (),
        ) -> None:
            gap = MetacognitiveJustificationGap.capture(
                kind=kind,
                decision=decision,
                evidence_reference=evidence_reference,
                detail=detail,
                affected_step_ids=affected_step_ids,
            )
            gaps.append(gap)
            findings.append(
                MetacognitiveSupervisionFinding.from_context(
                    context=context,
                    kind=MetacognitiveFindingKind.EVIDENCE_GAP,
                    risk_level=MetacognitiveRiskLevel.HIGH,
                    summary=detail,
                    evidence_references=(evidence_reference,),
                    affected_step_ids=affected_step_ids,
                    confidence_bp=policy.finding_confidence_bp,
                )
            )

        if len(rationale) < policy.minimum_rationale_characters:
            add_gap(
                kind=MetacognitiveJustificationGapKind.RATIONALE_TOO_SHORT,
                detail=(
                    "Decision rationale is shorter than the configured "
                    f"{policy.minimum_rationale_characters}-character minimum."
                ),
            )

        cited_evidence = tuple(
            reference
            for reference in context.evidence_references
            if _contains_exact(rationale, reference)
        )
        if (
            len(cited_evidence)
            < policy.minimum_cited_evidence_references
        ):
            add_gap(
                kind=(
                    MetacognitiveJustificationGapKind
                    .INSUFFICIENT_EVIDENCE_CITATIONS
                ),
                detail=(
                    "Decision rationale cites "
                    f"{len(cited_evidence)} context evidence reference(s); "
                    f"{policy.minimum_cited_evidence_references} required."
                ),
            )

        if policy.require_all_finding_citations:
            for finding in decision.findings:
                finding_hash = finding.finding_hash or ""
                if (
                    _contains_exact(rationale, finding.finding_id)
                    or _contains_exact(rationale, finding_hash)
                ):
                    continue
                add_gap(
                    kind=(
                        MetacognitiveJustificationGapKind
                        .MISSING_FINDING_CITATION
                    ),
                    detail=(
                        "Decision rationale does not cite finding "
                        f"{finding.finding_id} or its hash."
                    ),
                    affected_step_ids=finding.affected_step_ids,
                )

        if policy.require_corrective_step_citations:
            for step_id in decision.corrective_step_ids:
                if _contains_exact(rationale, step_id):
                    continue
                add_gap(
                    kind=(
                        MetacognitiveJustificationGapKind
                        .MISSING_CORRECTIVE_STEP_CITATION
                    ),
                    detail=(
                        "Decision rationale does not cite corrective step "
                        f"{step_id}."
                    ),
                    affected_step_ids=(step_id,),
                )

        if (
            policy.require_approval_reference_when_required
            and decision.approval_required
            and decision.approval_reference is None
        ):
            add_gap(
                kind=(
                    MetacognitiveJustificationGapKind
                    .MISSING_APPROVAL_REFERENCE
                ),
                detail=(
                    "Decision requires approval but has no approval_reference."
                ),
            )
        elif (
            policy.require_approval_reference_citation
            and decision.approval_required
            and decision.approval_reference is not None
            and not _contains_exact(rationale, decision.approval_reference)
        ):
            add_gap(
                kind=(
                    MetacognitiveJustificationGapKind
                    .MISSING_APPROVAL_REFERENCE_CITATION
                ),
                detail=(
                    "Decision rationale does not cite required approval "
                    f"reference {decision.approval_reference}."
                ),
            )

        return MetacognitiveInsufficientJustificationDetectionResult.capture(
            request=request,
            gaps=gaps,
            findings=findings,
        )


def detect_insufficient_justification(
    request: MetacognitiveInsufficientJustificationDetectionRequest,
) -> MetacognitiveInsufficientJustificationDetectionResult:
    """Detect insufficient justification without mutating system state."""

    return MetacognitiveInsufficientJustificationDetector().detect(request)
