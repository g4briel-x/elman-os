"""Deterministic metacognitive supervision decision contracts for ELMAN-OS v0.7.

This module defines immutable, hash-bound contracts for declaring a
metacognitive supervision context, findings, and one of five decisions:
continue, correct, pause, stop, or escalate.

The boundary is deliberately declarative. It does not mutate an execution plan,
append to a journal, persist state, dispatch an agent, invoke an AI provider, or
perform network access.
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


METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetacognitiveSupervisionDecisionError(ValueError):
    """A metacognitive supervision decision contract is malformed."""


class MetacognitiveSupervisionDecisionIntegrityError(
    MetacognitiveSupervisionDecisionError
):
    """A serialized contract fails deterministic integrity verification."""


class MetacognitiveSupervisionDecisionPolicyError(
    MetacognitiveSupervisionDecisionError
):
    """A requested decision violates the configured supervision policy."""


class MetacognitiveDecisionAction(StrEnum):
    CONTINUE = "continue"
    CORRECT = "correct"
    PAUSE = "pause"
    STOP = "stop"
    ESCALATE = "escalate"


class MetacognitiveRiskLevel(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MetacognitiveFindingKind(StrEnum):
    LOOP = "loop"
    CONTRADICTION = "contradiction"
    UNCERTAINTY = "uncertainty"
    POLICY_VIOLATION = "policy-violation"
    EVIDENCE_GAP = "evidence-gap"
    STALL = "stall"
    RESOURCE_RISK = "resource-risk"
    OTHER = "other"


_RISK_RANK: Final[dict[MetacognitiveRiskLevel, int]] = {
    MetacognitiveRiskLevel.INFO: 0,
    MetacognitiveRiskLevel.LOW: 1,
    MetacognitiveRiskLevel.MEDIUM: 2,
    MetacognitiveRiskLevel.HIGH: 3,
    MetacognitiveRiskLevel.CRITICAL: 4,
}


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveSupervisionDecisionError(
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
        raise MetacognitiveSupervisionDecisionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must be a boolean"
        )
    return value


def _basis_points(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 10_000
    ):
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must be an integer between 0 and 10000"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveSupervisionDecisionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveSupervisionDecisionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveSupervisionDecisionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveSupervisionDecisionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveSupervisionDecisionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _identifiers(
    values: Iterable[object],
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must be an iterable"
        )
    normalized = tuple(
        sorted({_identifier(value, name, pattern) for value in values})
    )
    if required and not normalized:
        raise MetacognitiveSupervisionDecisionError(
            f"{name} must contain at least one value"
        )
    return normalized


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveSupervisionDecisionPolicy:
    policy_id: str
    minimum_continue_confidence_bp: int = 7000
    minimum_correct_confidence_bp: int = 5000
    require_approval_for_pause: bool = True
    require_approval_for_stop: bool = True
    require_approval_for_escalate: bool = True
    fail_closed: bool = True
    version: int = METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "minimum_continue_confidence_bp",
            _basis_points(
                self.minimum_continue_confidence_bp,
                "minimum_continue_confidence_bp",
            ),
        )
        object.__setattr__(
            self,
            "minimum_correct_confidence_bp",
            _basis_points(
                self.minimum_correct_confidence_bp,
                "minimum_correct_confidence_bp",
            ),
        )
        if self.minimum_continue_confidence_bp < self.minimum_correct_confidence_bp:
            raise MetacognitiveSupervisionDecisionPolicyError(
                "continue confidence threshold cannot be lower than correct threshold"
            )
        for name in (
            "require_approval_for_pause",
            "require_approval_for_stop",
            "require_approval_for_escalate",
            "fail_closed",
        ):
            object.__setattr__(self, name, _boolean(getattr(self, name), name))
        if not self.fail_closed:
            raise MetacognitiveSupervisionDecisionPolicyError(
                "metacognitive supervision policy must fail closed"
            )
        if self.version != METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION:
            raise MetacognitiveSupervisionDecisionError(
                "unsupported metacognitive supervision decision format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_supervision_decision_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "minimum_continue_confidence_bp": (
                self.minimum_continue_confidence_bp
            ),
            "minimum_correct_confidence_bp": (
                self.minimum_correct_confidence_bp
            ),
            "require_approval_for_pause": self.require_approval_for_pause,
            "require_approval_for_stop": self.require_approval_for_stop,
            "require_approval_for_escalate": (
                self.require_approval_for_escalate
            ),
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
    ) -> "MetacognitiveSupervisionDecisionPolicy":
        if data.get("record_type") != "metacognitive_supervision_decision_policy":
            raise MetacognitiveSupervisionDecisionError(
                "record_type must be metacognitive_supervision_decision_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            minimum_continue_confidence_bp=data[
                "minimum_continue_confidence_bp"
            ],
            minimum_correct_confidence_bp=data[
                "minimum_correct_confidence_bp"
            ],
            require_approval_for_pause=data["require_approval_for_pause"],
            require_approval_for_stop=data["require_approval_for_stop"],
            require_approval_for_escalate=data[
                "require_approval_for_escalate"
            ],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveSupervisionDecisionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive supervision policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive supervision policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveSupervisionContext:
    context_id: str
    plan_id: str
    project_id: str
    plan_state_hash: str
    journal_hash: str
    checkpoint_hash: str
    evidence_references: tuple[str, ...]
    observed_by: str
    observed_at: str
    objective: str
    context_hash: str | None = None
    version: int = METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("context_id", "plan_id", "project_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        for name in ("plan_state_hash", "journal_hash", "checkpoint_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        object.__setattr__(
            self,
            "evidence_references",
            _identifiers(
                self.evidence_references,
                "evidence_reference",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "observed_by",
            _identifier(self.observed_by, "observed_by", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "observed_at",
            _utc_timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "objective",
            _text(self.objective, "objective"),
        )
        if self.version != METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION:
            raise MetacognitiveSupervisionDecisionError(
                "unsupported metacognitive supervision context format version"
            )
        expected_id = f"metacognitive-context:{self.compute_identity_hash()}"
        if self.context_id != expected_id:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "context_id does not match context identity"
            )
        computed = self.compute_hash()
        if self.context_hash is None:
            object.__setattr__(self, "context_hash", computed)
        else:
            supplied = _hash(self.context_hash, "context_hash")
            if supplied != computed:
                raise MetacognitiveSupervisionDecisionIntegrityError(
                    "context_hash does not match context content"
                )
            object.__setattr__(self, "context_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "plan_state_hash": self.plan_state_hash,
            "journal_hash": self.journal_hash,
            "checkpoint_hash": self.checkpoint_hash,
            "evidence_references": list(self.evidence_references),
            "observed_by": self.observed_by,
            "observed_at": self.observed_at,
            "objective": self.objective,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_supervision_context",
            "version": self.version,
            "context_id": self.context_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.context_hash != self.compute_hash():
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "context_hash does not match context content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["context_hash"] = self.context_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        plan_id: str,
        project_id: str,
        plan_state_hash: str,
        journal_hash: str,
        checkpoint_hash: str,
        evidence_references: Iterable[str],
        observed_by: str,
        observed_at: str | datetime,
        objective: str,
    ) -> "MetacognitiveSupervisionContext":
        normalized_plan_id = _identifier(plan_id, "plan_id")
        normalized_project_id = _identifier(project_id, "project_id")
        normalized_plan_hash = _hash(plan_state_hash, "plan_state_hash")
        normalized_journal_hash = _hash(journal_hash, "journal_hash")
        normalized_checkpoint_hash = _hash(checkpoint_hash, "checkpoint_hash")
        normalized_evidence = _identifiers(
            evidence_references,
            "evidence_reference",
            required=True,
        )
        normalized_observed_by = _identifier(
            observed_by,
            "observed_by",
            _AGENT_ID,
        )
        normalized_observed_at = _utc_timestamp(observed_at, "observed_at")
        normalized_objective = _text(objective, "objective")
        identity = {
            "plan_id": normalized_plan_id,
            "project_id": normalized_project_id,
            "plan_state_hash": normalized_plan_hash,
            "journal_hash": normalized_journal_hash,
            "checkpoint_hash": normalized_checkpoint_hash,
            "evidence_references": list(normalized_evidence),
            "observed_by": normalized_observed_by,
            "observed_at": normalized_observed_at,
            "objective": normalized_objective,
        }
        return cls(
            context_id=f"metacognitive-context:{_sha256_document(identity)}",
            plan_id=normalized_plan_id,
            project_id=normalized_project_id,
            plan_state_hash=normalized_plan_hash,
            journal_hash=normalized_journal_hash,
            checkpoint_hash=normalized_checkpoint_hash,
            evidence_references=normalized_evidence,
            observed_by=normalized_observed_by,
            observed_at=normalized_observed_at,
            objective=normalized_objective,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveSupervisionContext":
        if data.get("record_type") != "metacognitive_supervision_context":
            raise MetacognitiveSupervisionDecisionError(
                "record_type must be metacognitive_supervision_context"
            )
        if "context_hash" not in data:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "serialized context is missing context_hash"
            )
        return cls(
            context_id=data["context_id"],
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            plan_state_hash=data["plan_state_hash"],
            journal_hash=data["journal_hash"],
            checkpoint_hash=data["checkpoint_hash"],
            evidence_references=tuple(data["evidence_references"]),
            observed_by=data["observed_by"],
            observed_at=data["observed_at"],
            objective=data["objective"],
            context_hash=data["context_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveSupervisionContext":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive supervision context JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive supervision context JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveSupervisionFinding:
    finding_id: str
    context_id: str
    context_hash: str
    kind: MetacognitiveFindingKind
    risk_level: MetacognitiveRiskLevel
    summary: str
    evidence_references: tuple[str, ...]
    affected_step_ids: tuple[str, ...]
    confidence_bp: int
    finding_hash: str | None = None
    version: int = METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "finding_id",
            _identifier(self.finding_id, "finding_id"),
        )
        object.__setattr__(
            self,
            "context_id",
            _identifier(self.context_id, "context_id"),
        )
        object.__setattr__(
            self,
            "context_hash",
            _hash(self.context_hash, "context_hash"),
        )
        try:
            kind = MetacognitiveFindingKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive finding kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind)
        try:
            risk = MetacognitiveRiskLevel(self.risk_level)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive risk level is invalid"
            ) from exc
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(
            self,
            "evidence_references",
            _identifiers(
                self.evidence_references,
                "evidence_reference",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "affected_step_ids",
            _identifiers(
                self.affected_step_ids,
                "affected_step_id",
                _STEP_ID,
            ),
        )
        object.__setattr__(
            self,
            "confidence_bp",
            _basis_points(self.confidence_bp, "confidence_bp"),
        )
        if self.version != METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION:
            raise MetacognitiveSupervisionDecisionError(
                "unsupported metacognitive finding format version"
            )
        expected_id = f"metacognitive-finding:{self.compute_identity_hash()}"
        if self.finding_id != expected_id:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "finding_id does not match finding identity"
            )
        computed = self.compute_hash()
        if self.finding_hash is None:
            object.__setattr__(self, "finding_hash", computed)
        else:
            supplied = _hash(self.finding_hash, "finding_hash")
            if supplied != computed:
                raise MetacognitiveSupervisionDecisionIntegrityError(
                    "finding_hash does not match finding content"
                )
            object.__setattr__(self, "finding_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "context_hash": self.context_hash,
            "kind": self.kind.value,
            "risk_level": self.risk_level.value,
            "summary": self.summary,
            "evidence_references": list(self.evidence_references),
            "affected_step_ids": list(self.affected_step_ids),
            "confidence_bp": self.confidence_bp,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_supervision_finding",
            "version": self.version,
            "finding_id": self.finding_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.finding_hash != self.compute_hash():
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "finding_hash does not match finding content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["finding_hash"] = self.finding_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_context(
        cls,
        *,
        context: MetacognitiveSupervisionContext,
        kind: MetacognitiveFindingKind | str,
        risk_level: MetacognitiveRiskLevel | str,
        summary: str,
        evidence_references: Iterable[str],
        affected_step_ids: Iterable[str] = (),
        confidence_bp: int,
    ) -> "MetacognitiveSupervisionFinding":
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveSupervisionDecisionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        context.verify_hash()
        normalized_kind = MetacognitiveFindingKind(kind)
        normalized_risk = MetacognitiveRiskLevel(risk_level)
        normalized_summary = _text(summary, "summary")
        normalized_evidence = _identifiers(
            evidence_references,
            "evidence_reference",
            required=True,
        )
        missing = set(normalized_evidence) - set(context.evidence_references)
        if missing:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "finding evidence is not bound to the supervision context"
            )
        normalized_steps = _identifiers(
            affected_step_ids,
            "affected_step_id",
            _STEP_ID,
        )
        normalized_confidence = _basis_points(confidence_bp, "confidence_bp")
        identity = {
            "context_id": context.context_id,
            "context_hash": context.context_hash,
            "kind": normalized_kind.value,
            "risk_level": normalized_risk.value,
            "summary": normalized_summary,
            "evidence_references": list(normalized_evidence),
            "affected_step_ids": list(normalized_steps),
            "confidence_bp": normalized_confidence,
        }
        return cls(
            finding_id=f"metacognitive-finding:{_sha256_document(identity)}",
            context_id=context.context_id,
            context_hash=context.context_hash or "",
            kind=normalized_kind,
            risk_level=normalized_risk,
            summary=normalized_summary,
            evidence_references=normalized_evidence,
            affected_step_ids=normalized_steps,
            confidence_bp=normalized_confidence,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveSupervisionFinding":
        if data.get("record_type") != "metacognitive_supervision_finding":
            raise MetacognitiveSupervisionDecisionError(
                "record_type must be metacognitive_supervision_finding"
            )
        if "finding_hash" not in data:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "serialized finding is missing finding_hash"
            )
        return cls(
            finding_id=data["finding_id"],
            context_id=data["context_id"],
            context_hash=data["context_hash"],
            kind=data["kind"],
            risk_level=data["risk_level"],
            summary=data["summary"],
            evidence_references=tuple(data["evidence_references"]),
            affected_step_ids=tuple(data["affected_step_ids"]),
            confidence_bp=data["confidence_bp"],
            finding_hash=data["finding_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveSupervisionFinding":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive finding JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive finding JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveSupervisionDecision:
    decision_id: str
    policy_json: str
    policy_hash: str
    context_json: str
    context_hash: str
    findings_json: tuple[str, ...]
    finding_hashes: tuple[str, ...]
    action: MetacognitiveDecisionAction
    confidence_bp: int
    approval_required: bool
    approval_reference: str | None
    corrective_step_ids: tuple[str, ...]
    decided_by: str
    decided_at: str
    rationale: str
    decision_hash: str | None = None
    version: int = METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            _identifier(self.decision_id, "decision_id"),
        )
        policy = MetacognitiveSupervisionDecisionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        context = MetacognitiveSupervisionContext.from_json(
            _text(self.context_json, "context_json")
        )
        context.verify_hash()
        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != context.context_hash:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "context_hash does not match embedded context"
            )
        object.__setattr__(self, "context_json", context.to_json())
        object.__setattr__(self, "context_hash", supplied_context_hash)

        raw_findings = tuple(self.findings_json)
        if not raw_findings:
            findings: tuple[MetacognitiveSupervisionFinding, ...] = ()
        else:
            findings = tuple(
                MetacognitiveSupervisionFinding.from_json(
                    _text(payload, "finding_json")
                )
                for payload in raw_findings
            )
        for finding in findings:
            finding.verify_hash()
            if (
                finding.context_id != context.context_id
                or finding.context_hash != context.context_hash
            ):
                raise MetacognitiveSupervisionDecisionIntegrityError(
                    "finding is not bound to embedded supervision context"
                )
        findings = tuple(sorted(findings, key=lambda item: item.finding_id))
        hashes = tuple(item.finding_hash or "" for item in findings)
        supplied_hashes = tuple(
            sorted(_hash(value, "finding_hash") for value in self.finding_hashes)
        )
        if supplied_hashes != tuple(sorted(hashes)):
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "finding_hashes do not match embedded findings"
            )
        object.__setattr__(
            self,
            "findings_json",
            tuple(item.to_json() for item in findings),
        )
        object.__setattr__(self, "finding_hashes", hashes)

        try:
            action = MetacognitiveDecisionAction(self.action)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive decision action is invalid"
            ) from exc
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self,
            "confidence_bp",
            _basis_points(self.confidence_bp, "confidence_bp"),
        )
        object.__setattr__(
            self,
            "approval_required",
            _boolean(self.approval_required, "approval_required"),
        )
        if self.approval_reference is not None:
            normalized_reference = _identifier(
                self.approval_reference,
                "approval_reference",
            )
            if not self.approval_required:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "approval_reference is forbidden when approval is not required"
                )
            object.__setattr__(
                self,
                "approval_reference",
                normalized_reference,
            )
        object.__setattr__(
            self,
            "corrective_step_ids",
            _identifiers(
                self.corrective_step_ids,
                "corrective_step_id",
                _STEP_ID,
            ),
        )
        object.__setattr__(
            self,
            "decided_by",
            _identifier(self.decided_by, "decided_by", _AGENT_ID),
        )
        decided_at = _utc_timestamp(self.decided_at, "decided_at")
        if decided_at < context.observed_at:
            raise MetacognitiveSupervisionDecisionPolicyError(
                "decision cannot precede supervision observation"
            )
        object.__setattr__(self, "decided_at", decided_at)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

        if self.version != METACOGNITIVE_SUPERVISION_DECISION_FORMAT_VERSION:
            raise MetacognitiveSupervisionDecisionError(
                "unsupported metacognitive supervision decision format version"
            )
        self._validate_policy(policy, findings)

        expected_id = f"metacognitive-decision:{self.compute_identity_hash()}"
        if self.decision_id != expected_id:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "decision_id does not match decision identity"
            )
        computed = self.compute_hash()
        if self.decision_hash is None:
            object.__setattr__(self, "decision_hash", computed)
        else:
            supplied = _hash(self.decision_hash, "decision_hash")
            if supplied != computed:
                raise MetacognitiveSupervisionDecisionIntegrityError(
                    "decision_hash does not match decision content"
                )
            object.__setattr__(self, "decision_hash", supplied)

    @property
    def policy(self) -> MetacognitiveSupervisionDecisionPolicy:
        return MetacognitiveSupervisionDecisionPolicy.from_json(self.policy_json)

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.from_json(self.context_json)

    @property
    def findings(self) -> tuple[MetacognitiveSupervisionFinding, ...]:
        return tuple(
            MetacognitiveSupervisionFinding.from_json(payload)
            for payload in self.findings_json
        )

    @property
    def highest_risk(self) -> MetacognitiveRiskLevel:
        if not self.findings:
            return MetacognitiveRiskLevel.INFO
        return max(
            (item.risk_level for item in self.findings),
            key=lambda level: _RISK_RANK[level],
        )

    def _validate_policy(
        self,
        policy: MetacognitiveSupervisionDecisionPolicy,
        findings: tuple[MetacognitiveSupervisionFinding, ...],
    ) -> None:
        highest = (
            max(
                (item.risk_level for item in findings),
                key=lambda level: _RISK_RANK[level],
            )
            if findings
            else MetacognitiveRiskLevel.INFO
        )
        action = self.action

        if highest is MetacognitiveRiskLevel.CRITICAL and action not in {
            MetacognitiveDecisionAction.STOP,
            MetacognitiveDecisionAction.ESCALATE,
        }:
            raise MetacognitiveSupervisionDecisionPolicyError(
                "critical findings require stop or escalate"
            )

        if action is MetacognitiveDecisionAction.CONTINUE:
            if _RISK_RANK[highest] > _RISK_RANK[MetacognitiveRiskLevel.LOW]:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "continue is forbidden for medium, high, or critical risk"
                )
            if self.confidence_bp < policy.minimum_continue_confidence_bp:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "continue confidence is below policy threshold"
                )
            if self.approval_required or self.corrective_step_ids:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "continue cannot require approval or corrective steps"
                )

        elif action is MetacognitiveDecisionAction.CORRECT:
            if not findings:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "correct requires at least one finding"
                )
            if not self.corrective_step_ids:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "correct requires corrective_step_ids"
                )
            if highest is MetacognitiveRiskLevel.CRITICAL:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "correct is forbidden for critical risk"
                )
            if self.confidence_bp < policy.minimum_correct_confidence_bp:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "correct confidence is below policy threshold"
                )
            if self.approval_required:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "correct is declarative and cannot require approval"
                )

        elif action is MetacognitiveDecisionAction.PAUSE:
            if _RISK_RANK[highest] < _RISK_RANK[MetacognitiveRiskLevel.MEDIUM]:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "pause requires at least medium risk"
                )
            if self.corrective_step_ids:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "pause cannot declare corrective_step_ids"
                )
            if self.approval_required != policy.require_approval_for_pause:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "pause approval requirement differs from policy"
                )

        elif action is MetacognitiveDecisionAction.STOP:
            if _RISK_RANK[highest] < _RISK_RANK[MetacognitiveRiskLevel.HIGH]:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "stop requires high or critical risk"
                )
            if self.corrective_step_ids:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "stop cannot declare corrective_step_ids"
                )
            if self.approval_required != policy.require_approval_for_stop:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "stop approval requirement differs from policy"
                )

        elif action is MetacognitiveDecisionAction.ESCALATE:
            if _RISK_RANK[highest] < _RISK_RANK[MetacognitiveRiskLevel.MEDIUM]:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "escalate requires at least medium risk"
                )
            if self.corrective_step_ids:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "escalate cannot declare corrective_step_ids"
                )
            if self.approval_required != policy.require_approval_for_escalate:
                raise MetacognitiveSupervisionDecisionPolicyError(
                    "escalate approval requirement differs from policy"
                )

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "context_hash": self.context_hash,
            "finding_hashes": list(self.finding_hashes),
            "action": self.action.value,
            "confidence_bp": self.confidence_bp,
            "approval_required": self.approval_required,
            "approval_reference": self.approval_reference,
            "corrective_step_ids": list(self.corrective_step_ids),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "rationale": self.rationale,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_supervision_decision",
            "version": self.version,
            "decision_id": self.decision_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "context_json": self.context_json,
            "context_hash": self.context_hash,
            "findings_json": list(self.findings_json),
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.decision_hash != self.compute_hash():
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "decision_hash does not match decision content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["decision_hash"] = self.decision_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def declare(
        cls,
        *,
        policy: MetacognitiveSupervisionDecisionPolicy,
        context: MetacognitiveSupervisionContext,
        findings: Iterable[MetacognitiveSupervisionFinding],
        action: MetacognitiveDecisionAction | str,
        confidence_bp: int,
        approval_required: bool,
        approval_reference: str | None = None,
        corrective_step_ids: Iterable[str] = (),
        decided_by: str,
        decided_at: str | datetime,
        rationale: str,
    ) -> "MetacognitiveSupervisionDecision":
        if not isinstance(policy, MetacognitiveSupervisionDecisionPolicy):
            raise MetacognitiveSupervisionDecisionError(
                "policy must be a MetacognitiveSupervisionDecisionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveSupervisionDecisionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        context.verify_hash()
        normalized_findings = tuple(findings)
        if not all(
            isinstance(item, MetacognitiveSupervisionFinding)
            for item in normalized_findings
        ):
            raise MetacognitiveSupervisionDecisionError(
                "findings must contain MetacognitiveSupervisionFinding values"
            )
        normalized_findings = tuple(
            sorted(normalized_findings, key=lambda item: item.finding_id)
        )
        normalized_action = MetacognitiveDecisionAction(action)
        normalized_confidence = _basis_points(confidence_bp, "confidence_bp")
        normalized_approval_required = _boolean(
            approval_required,
            "approval_required",
        )
        normalized_corrective_steps = _identifiers(
            corrective_step_ids,
            "corrective_step_id",
            _STEP_ID,
        )
        normalized_decided_by = _identifier(
            decided_by,
            "decided_by",
            _AGENT_ID,
        )
        normalized_decided_at = _utc_timestamp(decided_at, "decided_at")
        normalized_rationale = _text(rationale, "rationale")
        normalized_approval_reference = (
            None
            if approval_reference is None
            else _identifier(approval_reference, "approval_reference")
        )
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "finding_hashes": [
                item.finding_hash for item in normalized_findings
            ],
            "action": normalized_action.value,
            "confidence_bp": normalized_confidence,
            "approval_required": normalized_approval_required,
            "approval_reference": normalized_approval_reference,
            "corrective_step_ids": list(normalized_corrective_steps),
            "decided_by": normalized_decided_by,
            "decided_at": normalized_decided_at,
            "rationale": normalized_rationale,
        }
        return cls(
            decision_id=f"metacognitive-decision:{_sha256_document(identity)}",
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            findings_json=tuple(
                item.to_json() for item in normalized_findings
            ),
            finding_hashes=tuple(
                item.finding_hash or "" for item in normalized_findings
            ),
            action=normalized_action,
            confidence_bp=normalized_confidence,
            approval_required=normalized_approval_required,
            approval_reference=normalized_approval_reference,
            corrective_step_ids=normalized_corrective_steps,
            decided_by=normalized_decided_by,
            decided_at=normalized_decided_at,
            rationale=normalized_rationale,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveSupervisionDecision":
        if data.get("record_type") != "metacognitive_supervision_decision":
            raise MetacognitiveSupervisionDecisionError(
                "record_type must be metacognitive_supervision_decision"
            )
        if "decision_hash" not in data:
            raise MetacognitiveSupervisionDecisionIntegrityError(
                "serialized decision is missing decision_hash"
            )
        return cls(
            decision_id=data["decision_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            context_json=data["context_json"],
            context_hash=data["context_hash"],
            findings_json=tuple(data["findings_json"]),
            finding_hashes=tuple(data["finding_hashes"]),
            action=data["action"],
            confidence_bp=data["confidence_bp"],
            approval_required=data["approval_required"],
            approval_reference=data.get("approval_reference"),
            corrective_step_ids=tuple(data["corrective_step_ids"]),
            decided_by=data["decided_by"],
            decided_at=data["decided_at"],
            rationale=data["rationale"],
            decision_hash=data["decision_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveSupervisionDecision":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive decision JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveSupervisionDecisionError(
                "metacognitive decision JSON must be an object"
            )
        return cls.from_dict(data)
