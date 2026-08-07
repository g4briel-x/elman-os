"""Deterministic metacognitive confidence reporting for ELMAN-OS v0.7.

The reporter consumes a hash-bound metacognitive supervision context and zero
or more validated supervision findings. It produces one immutable confidence
report expressed in basis points.

Confidence is structural confidence in the bound supervision evidence. It is
not a probability that an execution result is correct, and it never authorizes
an orchestration action.

The boundary is deliberately read-only and declarative. It does not mutate an
execution plan, append to a journal, persist state, dispatch an agent, invoke an
AI provider, apply a supervision decision, or perform network access.
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
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecisionError,
    MetacognitiveSupervisionFinding,
)


METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetacognitiveConfidenceReportError(ValueError):
    """A confidence-report contract or operation is invalid."""


class MetacognitiveConfidenceReportIntegrityError(
    MetacognitiveConfidenceReportError
):
    """A confidence-report document or binding fails integrity verification."""


class MetacognitiveConfidenceReportPolicyError(
    MetacognitiveConfidenceReportError
):
    """A confidence-report policy is unsafe or internally inconsistent."""


class MetacognitiveConfidenceLevel(StrEnum):
    """Human-readable band for a deterministic basis-point confidence score."""

    INSUFFICIENT = "insufficient"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveConfidenceReportError(
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
        raise MetacognitiveConfidenceReportError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveConfidenceReportError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveConfidenceReportError(f"{name} must be a boolean")
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
        raise MetacognitiveConfidenceReportError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _basis_points(value: object, name: str) -> int:
    return _integer(value, name, minimum=0, maximum=10_000)


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveConfidenceReportError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveConfidenceReportError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveConfidenceReportError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveConfidenceReportError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveConfidenceReportError(f"{name} must be UTC")
    else:
        raise MetacognitiveConfidenceReportError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _confidence_level(
    score_bp: int,
    policy: "MetacognitiveConfidenceReportPolicy",
) -> MetacognitiveConfidenceLevel:
    if score_bp < policy.low_confidence_threshold_bp:
        return MetacognitiveConfidenceLevel.INSUFFICIENT
    if score_bp < policy.medium_confidence_threshold_bp:
        return MetacognitiveConfidenceLevel.LOW
    if score_bp < policy.high_confidence_threshold_bp:
        return MetacognitiveConfidenceLevel.MEDIUM
    return MetacognitiveConfidenceLevel.HIGH


@dataclass(frozen=True, slots=True)
class MetacognitiveConfidenceReportPolicy:
    """Fail-closed thresholds and caps for structural confidence reporting."""

    policy_id: str
    low_confidence_threshold_bp: int = 4000
    medium_confidence_threshold_bp: int = 6000
    high_confidence_threshold_bp: int = 8000
    uncertainty_cap_bp: int = 5999
    evidence_gap_cap_bp: int = 4999
    minimum_findings: int = 1
    fail_closed: bool = True
    version: int = METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for name in (
            "low_confidence_threshold_bp",
            "medium_confidence_threshold_bp",
            "high_confidence_threshold_bp",
            "uncertainty_cap_bp",
            "evidence_gap_cap_bp",
        ):
            object.__setattr__(
                self,
                name,
                _basis_points(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "minimum_findings",
            _integer(
                self.minimum_findings,
                "minimum_findings",
                minimum=1,
                maximum=100_000,
            ),
        )
        object.__setattr__(
            self,
            "fail_closed",
            _boolean(self.fail_closed, "fail_closed"),
        )

        if not (
            0
            < self.low_confidence_threshold_bp
            < self.medium_confidence_threshold_bp
            < self.high_confidence_threshold_bp
            <= 10_000
        ):
            raise MetacognitiveConfidenceReportPolicyError(
                "confidence thresholds must be strictly increasing"
            )
        if self.evidence_gap_cap_bp > self.uncertainty_cap_bp:
            raise MetacognitiveConfidenceReportPolicyError(
                "evidence-gap cap cannot exceed uncertainty cap"
            )
        if not self.fail_closed:
            raise MetacognitiveConfidenceReportPolicyError(
                "metacognitive confidence reporting must fail closed"
            )
        if self.version != METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION:
            raise MetacognitiveConfidenceReportError(
                "unsupported metacognitive confidence-report format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_confidence_report_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "low_confidence_threshold_bp": self.low_confidence_threshold_bp,
            "medium_confidence_threshold_bp": (
                self.medium_confidence_threshold_bp
            ),
            "high_confidence_threshold_bp": self.high_confidence_threshold_bp,
            "uncertainty_cap_bp": self.uncertainty_cap_bp,
            "evidence_gap_cap_bp": self.evidence_gap_cap_bp,
            "minimum_findings": self.minimum_findings,
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
    ) -> "MetacognitiveConfidenceReportPolicy":
        if data.get("record_type") != "metacognitive_confidence_report_policy":
            raise MetacognitiveConfidenceReportError(
                "record_type must be metacognitive_confidence_report_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            low_confidence_threshold_bp=data["low_confidence_threshold_bp"],
            medium_confidence_threshold_bp=data[
                "medium_confidence_threshold_bp"
            ],
            high_confidence_threshold_bp=data["high_confidence_threshold_bp"],
            uncertainty_cap_bp=data["uncertainty_cap_bp"],
            evidence_gap_cap_bp=data["evidence_gap_cap_bp"],
            minimum_findings=data["minimum_findings"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveConfidenceReportPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveConfidenceReportError(
                "confidence-report policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveConfidenceReportError(
                "confidence-report policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveConfidenceReportRequest:
    """Hash-bound request containing every input used by the reporter."""

    request_id: str
    policy_json: str
    policy_hash: str
    context_json: str
    context_hash: str
    findings_json: tuple[str, ...]
    finding_hashes: tuple[str, ...]
    requested_by: str
    requested_at: str
    reason: str
    request_hash: str | None = None
    version: int = METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )

        policy = MetacognitiveConfidenceReportPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveConfidenceReportIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        try:
            context = MetacognitiveSupervisionContext.from_json(
                _text(self.context_json, "context_json")
            )
            context.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveConfidenceReportIntegrityError(
                "embedded supervision context is invalid"
            ) from exc

        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != context.context_hash:
            raise MetacognitiveConfidenceReportIntegrityError(
                "context_hash does not match embedded context"
            )
        object.__setattr__(self, "context_json", context.to_json())
        object.__setattr__(self, "context_hash", supplied_context_hash)

        raw_findings = tuple(self.findings_json)
        findings: list[MetacognitiveSupervisionFinding] = []
        try:
            for payload in raw_findings:
                finding = MetacognitiveSupervisionFinding.from_json(
                    _text(payload, "finding_json")
                )
                finding.verify_hash()
                if (
                    finding.context_id != context.context_id
                    or finding.context_hash != context.context_hash
                ):
                    raise MetacognitiveConfidenceReportIntegrityError(
                        "finding is not bound to embedded supervision context"
                    )
                findings.append(finding)
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveConfidenceReportIntegrityError(
                "embedded supervision finding is invalid"
            ) from exc

        findings.sort(key=lambda item: item.finding_id)
        finding_ids = tuple(item.finding_id for item in findings)
        if len(set(finding_ids)) != len(finding_ids):
            raise MetacognitiveConfidenceReportIntegrityError(
                "duplicate supervision finding is forbidden"
            )

        hashes = tuple(item.finding_hash or "" for item in findings)
        supplied_hashes = tuple(
            sorted(_hash(value, "finding_hash") for value in self.finding_hashes)
        )
        if supplied_hashes != tuple(sorted(hashes)):
            raise MetacognitiveConfidenceReportIntegrityError(
                "finding_hashes do not match embedded findings"
            )

        object.__setattr__(
            self,
            "findings_json",
            tuple(item.to_json() for item in findings),
        )
        object.__setattr__(self, "finding_hashes", hashes)

        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < context.observed_at:
            raise MetacognitiveConfidenceReportPolicyError(
                "confidence-report request cannot precede context observation"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

        if self.version != METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION:
            raise MetacognitiveConfidenceReportError(
                "unsupported metacognitive confidence-report request version"
            )

        expected_id = f"metacognitive-confidence-request:{self.compute_identity_hash()}"
        if self.request_id != expected_id:
            raise MetacognitiveConfidenceReportIntegrityError(
                "request_id does not match confidence-report request identity"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveConfidenceReportIntegrityError(
                    "request_hash does not match confidence-report request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveConfidenceReportPolicy:
        return MetacognitiveConfidenceReportPolicy.from_json(self.policy_json)

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.from_json(self.context_json)

    @property
    def findings(self) -> tuple[MetacognitiveSupervisionFinding, ...]:
        return tuple(
            MetacognitiveSupervisionFinding.from_json(payload)
            for payload in self.findings_json
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "context_hash": self.context_hash,
            "finding_hashes": list(self.finding_hashes),
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_confidence_report_request",
            "version": self.version,
            "request_id": self.request_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "context_json": self.context_json,
            "context_hash": self.context_hash,
            "findings_json": list(self.findings_json),
            "finding_hashes": list(self.finding_hashes),
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise MetacognitiveConfidenceReportIntegrityError(
                "request_hash does not match confidence-report request content"
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
        policy: MetacognitiveConfidenceReportPolicy,
        context: MetacognitiveSupervisionContext,
        findings: Iterable[MetacognitiveSupervisionFinding],
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveConfidenceReportRequest":
        if not isinstance(policy, MetacognitiveConfidenceReportPolicy):
            raise MetacognitiveConfidenceReportError(
                "policy must be a MetacognitiveConfidenceReportPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveConfidenceReportError(
                "context must be a MetacognitiveSupervisionContext"
            )
        context.verify_hash()

        normalized_findings = tuple(findings)
        for finding in normalized_findings:
            if not isinstance(finding, MetacognitiveSupervisionFinding):
                raise MetacognitiveConfidenceReportError(
                    "findings must contain MetacognitiveSupervisionFinding values"
                )
            finding.verify_hash()
            if (
                finding.context_id != context.context_id
                or finding.context_hash != context.context_hash
            ):
                raise MetacognitiveConfidenceReportIntegrityError(
                    "finding is not bound to supplied supervision context"
                )
        normalized_findings = tuple(
            sorted(normalized_findings, key=lambda item: item.finding_id)
        )
        ids = tuple(item.finding_id for item in normalized_findings)
        if len(set(ids)) != len(ids):
            raise MetacognitiveConfidenceReportIntegrityError(
                "duplicate supervision finding is forbidden"
            )

        normalized_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_at = _utc_timestamp(requested_at, "requested_at")
        if normalized_at < context.observed_at:
            raise MetacognitiveConfidenceReportPolicyError(
                "confidence-report request cannot precede context observation"
            )
        normalized_reason = _text(reason, "reason")
        finding_hashes = tuple(item.finding_hash or "" for item in normalized_findings)

        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "finding_hashes": list(finding_hashes),
            "requested_by": normalized_by,
            "requested_at": normalized_at,
            "reason": normalized_reason,
        }

        return cls(
            request_id=(
                "metacognitive-confidence-request:"
                f"{_sha256_document(identity)}"
            ),
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            findings_json=tuple(
                item.to_json() for item in normalized_findings
            ),
            finding_hashes=finding_hashes,
            requested_by=normalized_by,
            requested_at=normalized_at,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveConfidenceReportRequest":
        if data.get("record_type") != "metacognitive_confidence_report_request":
            raise MetacognitiveConfidenceReportError(
                "record_type must be metacognitive_confidence_report_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveConfidenceReportIntegrityError(
                "serialized confidence-report request is missing request_hash"
            )
        return cls(
            request_id=data["request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            context_json=data["context_json"],
            context_hash=data["context_hash"],
            findings_json=tuple(data["findings_json"]),
            finding_hashes=tuple(data["finding_hashes"]),
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
    ) -> "MetacognitiveConfidenceReportRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveConfidenceReportError(
                "confidence-report request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveConfidenceReportError(
                "confidence-report request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class _ConfidenceMetrics:
    finding_count: int
    context_evidence_count: int
    covered_evidence_references: tuple[str, ...]
    finding_confidence_floor_bp: int
    evidence_coverage_bp: int
    applicable_cap_bp: int
    overall_confidence_bp: int
    confidence_level: MetacognitiveConfidenceLevel


def _compute_metrics(
    request: MetacognitiveConfidenceReportRequest,
) -> _ConfidenceMetrics:
    policy = request.policy
    context = request.context
    findings = request.findings

    context_evidence = tuple(sorted(context.evidence_references))
    context_evidence_set = set(context_evidence)
    covered = tuple(
        sorted(
            {
                reference
                for finding in findings
                for reference in finding.evidence_references
                if reference in context_evidence_set
            }
        )
    )

    if context_evidence:
        evidence_coverage_bp = (
            len(covered) * 10_000 // len(context_evidence)
        )
    else:
        # Existing supervision contexts require at least one evidence reference.
        # Keep a defensive fail-closed branch for future contract evolution.
        evidence_coverage_bp = 0

    finding_confidence_floor_bp = (
        min(finding.confidence_bp for finding in findings)
        if findings
        else 0
    )

    applicable_cap_bp = 10_000
    kinds = {finding.kind for finding in findings}
    if MetacognitiveFindingKind.UNCERTAINTY in kinds:
        applicable_cap_bp = min(
            applicable_cap_bp,
            policy.uncertainty_cap_bp,
        )
    if MetacognitiveFindingKind.EVIDENCE_GAP in kinds:
        applicable_cap_bp = min(
            applicable_cap_bp,
            policy.evidence_gap_cap_bp,
        )

    if len(findings) < policy.minimum_findings:
        overall_confidence_bp = 0
    else:
        overall_confidence_bp = min(
            finding_confidence_floor_bp,
            evidence_coverage_bp,
            applicable_cap_bp,
        )

    return _ConfidenceMetrics(
        finding_count=len(findings),
        context_evidence_count=len(context_evidence),
        covered_evidence_references=covered,
        finding_confidence_floor_bp=finding_confidence_floor_bp,
        evidence_coverage_bp=evidence_coverage_bp,
        applicable_cap_bp=applicable_cap_bp,
        overall_confidence_bp=overall_confidence_bp,
        confidence_level=_confidence_level(overall_confidence_bp, policy),
    )


def _rationale(metrics: _ConfidenceMetrics) -> str:
    return (
        "Structural confidence is the conservative minimum of the "
        "finding-confidence floor, bound-evidence coverage, and applicable "
        f"uncertainty/evidence-gap caps; findings={metrics.finding_count}, "
        f"covered-evidence={len(metrics.covered_evidence_references)}/"
        f"{metrics.context_evidence_count}, "
        f"score={metrics.overall_confidence_bp}bp, "
        f"level={metrics.confidence_level.value}."
    )


@dataclass(frozen=True, slots=True)
class MetacognitiveConfidenceReport:
    """Immutable, self-verifying report derived only from its embedded request."""

    report_id: str
    request_json: str
    request_hash: str
    finding_count: int
    context_evidence_count: int
    covered_evidence_references: tuple[str, ...]
    finding_confidence_floor_bp: int
    evidence_coverage_bp: int
    applicable_cap_bp: int
    overall_confidence_bp: int
    confidence_level: MetacognitiveConfidenceLevel
    generated_by: str
    generated_at: str
    rationale: str
    report_hash: str | None = None
    version: int = METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "report_id",
            _identifier(self.report_id, "report_id"),
        )
        request = MetacognitiveConfidenceReportRequest.from_json(
            _text(self.request_json, "request_json")
        )
        request.verify_hash()
        supplied_request_hash = _hash(self.request_hash, "request_hash")
        if supplied_request_hash != request.request_hash:
            raise MetacognitiveConfidenceReportIntegrityError(
                "request_hash does not match embedded confidence-report request"
            )
        object.__setattr__(self, "request_json", request.to_json())
        object.__setattr__(self, "request_hash", supplied_request_hash)

        metrics = _compute_metrics(request)
        for name, expected in (
            ("finding_count", metrics.finding_count),
            ("context_evidence_count", metrics.context_evidence_count),
            (
                "finding_confidence_floor_bp",
                metrics.finding_confidence_floor_bp,
            ),
            ("evidence_coverage_bp", metrics.evidence_coverage_bp),
            ("applicable_cap_bp", metrics.applicable_cap_bp),
            ("overall_confidence_bp", metrics.overall_confidence_bp),
        ):
            supplied = _integer(
                getattr(self, name),
                name,
                minimum=0,
                maximum=100_000 if name.endswith("_count") else 10_000,
            )
            if supplied != expected:
                raise MetacognitiveConfidenceReportIntegrityError(
                    f"{name} does not match deterministic confidence metrics"
                )
            object.__setattr__(self, name, supplied)

        normalized_covered = tuple(
            sorted(
                _identifier(value, "covered_evidence_reference")
                for value in self.covered_evidence_references
            )
        )
        if normalized_covered != metrics.covered_evidence_references:
            raise MetacognitiveConfidenceReportIntegrityError(
                "covered_evidence_references do not match deterministic metrics"
            )
        object.__setattr__(
            self,
            "covered_evidence_references",
            normalized_covered,
        )

        try:
            level = MetacognitiveConfidenceLevel(self.confidence_level)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveConfidenceReportError(
                "confidence_level is invalid"
            ) from exc
        if level != metrics.confidence_level:
            raise MetacognitiveConfidenceReportIntegrityError(
                "confidence_level does not match deterministic confidence score"
            )
        object.__setattr__(self, "confidence_level", level)

        generated_by = _identifier(
            self.generated_by,
            "generated_by",
            _AGENT_ID,
        )
        if generated_by != request.requested_by:
            raise MetacognitiveConfidenceReportIntegrityError(
                "generated_by must equal the request author for deterministic reporting"
            )
        object.__setattr__(self, "generated_by", generated_by)

        generated_at = _utc_timestamp(self.generated_at, "generated_at")
        if generated_at != request.requested_at:
            raise MetacognitiveConfidenceReportIntegrityError(
                "generated_at must equal requested_at for deterministic reporting"
            )
        object.__setattr__(self, "generated_at", generated_at)

        expected_rationale = _rationale(metrics)
        normalized_rationale = _text(self.rationale, "rationale")
        if normalized_rationale != expected_rationale:
            raise MetacognitiveConfidenceReportIntegrityError(
                "rationale does not match deterministic confidence metrics"
            )
        object.__setattr__(self, "rationale", normalized_rationale)

        if self.version != METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION:
            raise MetacognitiveConfidenceReportError(
                "unsupported metacognitive confidence-report version"
            )

        expected_id = f"metacognitive-confidence-report:{self.compute_identity_hash()}"
        if self.report_id != expected_id:
            raise MetacognitiveConfidenceReportIntegrityError(
                "report_id does not match confidence-report identity"
            )

        computed = self.compute_hash()
        if self.report_hash is None:
            object.__setattr__(self, "report_hash", computed)
        else:
            supplied_hash = _hash(self.report_hash, "report_hash")
            if supplied_hash != computed:
                raise MetacognitiveConfidenceReportIntegrityError(
                    "report_hash does not match confidence-report content"
                )
            object.__setattr__(self, "report_hash", supplied_hash)

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "overall_confidence_bp": self.overall_confidence_bp,
            "confidence_level": self.confidence_level.value,
            "generated_by": self.generated_by,
            "generated_at": self.generated_at,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_confidence_report",
            "version": self.version,
            "report_id": self.report_id,
            "request_json": self.request_json,
            "request_hash": self.request_hash,
            "finding_count": self.finding_count,
            "context_evidence_count": self.context_evidence_count,
            "covered_evidence_references": list(
                self.covered_evidence_references
            ),
            "finding_confidence_floor_bp": self.finding_confidence_floor_bp,
            "evidence_coverage_bp": self.evidence_coverage_bp,
            "applicable_cap_bp": self.applicable_cap_bp,
            "overall_confidence_bp": self.overall_confidence_bp,
            "confidence_level": self.confidence_level.value,
            "generated_by": self.generated_by,
            "generated_at": self.generated_at,
            "rationale": self.rationale,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.report_hash != self.compute_hash():
            raise MetacognitiveConfidenceReportIntegrityError(
                "report_hash does not match confidence-report content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["report_hash"] = self.report_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveConfidenceReport":
        if data.get("record_type") != "metacognitive_confidence_report":
            raise MetacognitiveConfidenceReportError(
                "record_type must be metacognitive_confidence_report"
            )
        if "report_hash" not in data:
            raise MetacognitiveConfidenceReportIntegrityError(
                "serialized confidence report is missing report_hash"
            )
        return cls(
            report_id=data["report_id"],
            request_json=data["request_json"],
            request_hash=data["request_hash"],
            finding_count=data["finding_count"],
            context_evidence_count=data["context_evidence_count"],
            covered_evidence_references=tuple(
                data["covered_evidence_references"]
            ),
            finding_confidence_floor_bp=data[
                "finding_confidence_floor_bp"
            ],
            evidence_coverage_bp=data["evidence_coverage_bp"],
            applicable_cap_bp=data["applicable_cap_bp"],
            overall_confidence_bp=data["overall_confidence_bp"],
            confidence_level=data["confidence_level"],
            generated_by=data["generated_by"],
            generated_at=data["generated_at"],
            rationale=data["rationale"],
            report_hash=data["report_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveConfidenceReport":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveConfidenceReportError(
                "confidence-report JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveConfidenceReportError(
                "confidence-report JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveConfidenceReporter:
    """Pure deterministic transformation from a verified request to a report."""

    def generate(
        self,
        request: MetacognitiveConfidenceReportRequest,
    ) -> MetacognitiveConfidenceReport:
        if not isinstance(request, MetacognitiveConfidenceReportRequest):
            raise MetacognitiveConfidenceReportError(
                "request must be a MetacognitiveConfidenceReportRequest"
            )
        request.verify_hash()
        metrics = _compute_metrics(request)
        identity = {
            "request_hash": request.request_hash,
            "overall_confidence_bp": metrics.overall_confidence_bp,
            "confidence_level": metrics.confidence_level.value,
            "generated_by": request.requested_by,
            "generated_at": request.requested_at,
        }
        return MetacognitiveConfidenceReport(
            report_id=(
                "metacognitive-confidence-report:"
                f"{_sha256_document(identity)}"
            ),
            request_json=request.to_json(),
            request_hash=request.request_hash or "",
            finding_count=metrics.finding_count,
            context_evidence_count=metrics.context_evidence_count,
            covered_evidence_references=metrics.covered_evidence_references,
            finding_confidence_floor_bp=metrics.finding_confidence_floor_bp,
            evidence_coverage_bp=metrics.evidence_coverage_bp,
            applicable_cap_bp=metrics.applicable_cap_bp,
            overall_confidence_bp=metrics.overall_confidence_bp,
            confidence_level=metrics.confidence_level,
            generated_by=request.requested_by,
            generated_at=request.requested_at,
            rationale=_rationale(metrics),
        )
