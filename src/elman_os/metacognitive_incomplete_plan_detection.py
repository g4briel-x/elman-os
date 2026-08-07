"""Deterministic incomplete-plan detection for ELMAN-OS v0.7.

The detector evaluates an already-valid ExecutionPlan against an explicit
completeness policy. It does not duplicate ExecutionPlan structural validation
and never guesses missing work from natural language.
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
from .execution_plan import ExecutionPlan
from .metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecisionError,
    MetacognitiveSupervisionFinding,
)

METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION: Final[int] = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetacognitiveIncompletePlanDetectionError(ValueError):
    """An incomplete-plan detection contract or operation is invalid."""


class MetacognitiveIncompletePlanDetectionIntegrityError(
    MetacognitiveIncompletePlanDetectionError
):
    """A document or binding fails deterministic integrity verification."""


class MetacognitiveIncompletePlanDetectionPolicyError(
    MetacognitiveIncompletePlanDetectionError
):
    """An incomplete-plan detection policy is unsafe or inconsistent."""


class MetacognitiveIncompletePlanDetectionStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class MetacognitiveIncompletePlanGapKind(StrEnum):
    MINIMUM_STEP_COUNT = "minimum-step-count"
    MISSING_REQUIRED_STEP = "missing-required-step"
    MISSING_REQUIRED_CAPABILITY = "missing-required-capability"
    UNBOUND_STEP = "unbound-step"
    MISSING_PLAN_APPROVAL = "missing-plan-approval"
    MISSING_STEP_APPROVAL = "missing-step-approval"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveIncompletePlanDetectionError(
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
        raise MetacognitiveIncompletePlanDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveIncompletePlanDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveIncompletePlanDetectionError(
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
        raise MetacognitiveIncompletePlanDetectionError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveIncompletePlanDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveIncompletePlanDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveIncompletePlanDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveIncompletePlanDetectionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveIncompletePlanDetectionError(
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
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MetacognitiveIncompletePlanDetectionError(
            f"{name} must be an iterable"
        )
    return tuple(
        sorted({_identifier(value, name, pattern) for value in values})
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _plan_state_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveIncompletePlanDetectionPolicy:
    policy_id: str
    required_step_ids: tuple[str, ...] = ()
    required_capability_ids: tuple[str, ...] = ()
    minimum_step_count: int = 1
    require_bound_agents: bool = False
    require_approval_trace: bool = False
    finding_confidence_bp: int = 9500
    fail_closed: bool = True
    version: int = METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "required_step_ids",
            _identifiers(
                self.required_step_ids,
                "required_step_id",
                _STEP_ID,
            ),
        )
        object.__setattr__(
            self,
            "required_capability_ids",
            _identifiers(
                self.required_capability_ids,
                "required_capability_id",
                _TOKEN,
            ),
        )
        object.__setattr__(
            self,
            "minimum_step_count",
            _integer(
                self.minimum_step_count,
                "minimum_step_count",
                minimum=1,
                maximum=10_000,
            ),
        )
        object.__setattr__(
            self,
            "require_bound_agents",
            _boolean(self.require_bound_agents, "require_bound_agents"),
        )
        object.__setattr__(
            self,
            "require_approval_trace",
            _boolean(self.require_approval_trace, "require_approval_trace"),
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
        object.__setattr__(
            self,
            "fail_closed",
            _boolean(self.fail_closed, "fail_closed"),
        )
        if not self.fail_closed:
            raise MetacognitiveIncompletePlanDetectionPolicyError(
                "incomplete-plan detection must fail closed"
            )
        if self.version != METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION:
            raise MetacognitiveIncompletePlanDetectionError(
                "unsupported incomplete-plan detection format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_incomplete_plan_detection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "required_step_ids": list(self.required_step_ids),
            "required_capability_ids": list(self.required_capability_ids),
            "minimum_step_count": self.minimum_step_count,
            "require_bound_agents": self.require_bound_agents,
            "require_approval_trace": self.require_approval_trace,
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
    ) -> "MetacognitiveIncompletePlanDetectionPolicy":
        if (
            data.get("record_type")
            != "metacognitive_incomplete_plan_detection_policy"
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "record_type must be metacognitive_incomplete_plan_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            required_step_ids=tuple(data["required_step_ids"]),
            required_capability_ids=tuple(data["required_capability_ids"]),
            minimum_step_count=data["minimum_step_count"],
            require_bound_agents=data["require_bound_agents"],
            require_approval_trace=data["require_approval_trace"],
            finding_confidence_bp=data["finding_confidence_bp"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveIncompletePlanDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveIncompletePlanDetectionRequest:
    request_id: str
    policy_json: str
    policy_hash: str
    context_json: str
    context_hash: str
    plan_json: str
    plan_state_hash: str
    plan_evidence_reference: str
    requested_by: str
    requested_at: str
    reason: str
    request_hash: str | None = None
    version: int = METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _identifier(self.request_id, "request_id"),
        )
        policy = MetacognitiveIncompletePlanDetectionPolicy.from_json(
            _text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
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
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "embedded supervision context is invalid"
            ) from exc
        supplied_context_hash = _hash(self.context_hash, "context_hash")
        if supplied_context_hash != context.context_hash:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "context_hash does not match embedded context"
            )
        object.__setattr__(self, "context_json", context.to_json())
        object.__setattr__(self, "context_hash", supplied_context_hash)

        try:
            plan = ExecutionPlan.from_json(_text(self.plan_json, "plan_json"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "embedded execution plan is invalid"
            ) from exc
        canonical_plan_json = plan.to_json()
        computed_plan_hash = _plan_state_hash(plan)
        supplied_plan_hash = _hash(self.plan_state_hash, "plan_state_hash")
        if supplied_plan_hash != computed_plan_hash:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "plan_state_hash does not match embedded plan"
            )
        if supplied_plan_hash != context.plan_state_hash:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "plan_state_hash does not match supervision context"
            )
        if plan.plan_id != context.plan_id:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "plan_id does not match supervision context"
            )
        if plan.project_id != context.project_id:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "project_id does not match supervision context"
            )
        object.__setattr__(self, "plan_json", canonical_plan_json)
        object.__setattr__(self, "plan_state_hash", supplied_plan_hash)

        evidence = _identifier(
            self.plan_evidence_reference,
            "plan_evidence_reference",
        )
        if evidence not in context.evidence_references:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "plan evidence reference is not bound to supervision context"
            )
        object.__setattr__(self, "plan_evidence_reference", evidence)

        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < context.observed_at:
            raise MetacognitiveIncompletePlanDetectionPolicyError(
                "incomplete-plan detection cannot precede context observation"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION:
            raise MetacognitiveIncompletePlanDetectionError(
                "unsupported incomplete-plan request format version"
            )
        expected_id = (
            f"metacognitive-incomplete-plan-request:{self.compute_identity_hash()}"
        )
        if self.request_id != expected_id:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveIncompletePlanDetectionPolicy:
        return MetacognitiveIncompletePlanDetectionPolicy.from_json(
            self.policy_json
        )

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.from_json(self.context_json)

    @property
    def plan(self) -> ExecutionPlan:
        return ExecutionPlan.from_json(self.plan_json)

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "context_hash": self.context_hash,
            "plan_state_hash": self.plan_state_hash,
            "plan_evidence_reference": self.plan_evidence_reference,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_incomplete_plan_detection_request",
            "version": self.version,
            "request_id": self.request_id,
            "policy_json": self.policy_json,
            "context_json": self.context_json,
            "plan_json": self.plan_json,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
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
        policy: MetacognitiveIncompletePlanDetectionPolicy,
        context: MetacognitiveSupervisionContext,
        plan: ExecutionPlan,
        plan_evidence_reference: str,
        requested_by: str,
        requested_at: str | datetime,
        reason: str,
    ) -> "MetacognitiveIncompletePlanDetectionRequest":
        if not isinstance(
            policy,
            MetacognitiveIncompletePlanDetectionPolicy,
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "policy must be a MetacognitiveIncompletePlanDetectionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveIncompletePlanDetectionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        if not isinstance(plan, ExecutionPlan):
            raise MetacognitiveIncompletePlanDetectionError(
                "plan must be an ExecutionPlan"
            )
        try:
            context.verify_hash()
        except MetacognitiveSupervisionDecisionError as exc:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "supervision context failed integrity verification"
            ) from exc

        evidence = _identifier(
            plan_evidence_reference,
            "plan_evidence_reference",
        )
        requested_by_value = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        requested_at_value = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        reason_value = _text(reason, "reason")
        plan_hash = _plan_state_hash(plan)
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "plan_state_hash": plan_hash,
            "plan_evidence_reference": evidence,
            "requested_by": requested_by_value,
            "requested_at": requested_at_value,
            "reason": reason_value,
        }
        return cls(
            request_id=(
                "metacognitive-incomplete-plan-request:"
                f"{_sha256_document(identity)}"
            ),
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            plan_json=plan.to_json(),
            plan_state_hash=plan_hash,
            plan_evidence_reference=evidence,
            requested_by=requested_by_value,
            requested_at=requested_at_value,
            reason=reason_value,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveIncompletePlanDetectionRequest":
        if (
            data.get("record_type")
            != "metacognitive_incomplete_plan_detection_request"
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "record_type must be metacognitive_incomplete_plan_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            request_id=data["request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            context_json=data["context_json"],
            context_hash=data["context_hash"],
            plan_json=data["plan_json"],
            plan_state_hash=data["plan_state_hash"],
            plan_evidence_reference=data["plan_evidence_reference"],
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
    ) -> "MetacognitiveIncompletePlanDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveIncompletePlanGap:
    gap_id: str
    kind: MetacognitiveIncompletePlanGapKind
    requirement: str
    summary: str
    affected_step_ids: tuple[str, ...]
    risk_level: MetacognitiveRiskLevel
    confidence_bp: int
    gap_hash: str | None = None
    version: int = METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gap_id",
            _identifier(self.gap_id, "gap_id"),
        )
        try:
            kind = MetacognitiveIncompletePlanGapKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan gap kind is invalid"
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "requirement",
            _text(self.requirement, "requirement"),
        )
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(
            self,
            "affected_step_ids",
            _identifiers(
                self.affected_step_ids,
                "affected_step_id",
                _STEP_ID,
            ),
        )
        try:
            risk = MetacognitiveRiskLevel(self.risk_level)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan gap risk level is invalid"
            ) from exc
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(
            self,
            "confidence_bp",
            _integer(
                self.confidence_bp,
                "confidence_bp",
                minimum=0,
                maximum=10_000,
            ),
        )
        if self.version != METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION:
            raise MetacognitiveIncompletePlanDetectionError(
                "unsupported incomplete-plan gap format version"
            )
        expected_id = f"metacognitive-plan-gap:{self.compute_identity_hash()}"
        if self.gap_id != expected_id:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "gap_id does not match gap identity"
            )
        computed = self.compute_hash()
        if self.gap_hash is None:
            object.__setattr__(self, "gap_hash", computed)
        else:
            supplied = _hash(self.gap_hash, "gap_hash")
            if supplied != computed:
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "gap_hash does not match gap content"
                )
            object.__setattr__(self, "gap_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "requirement": self.requirement,
            "summary": self.summary,
            "affected_step_ids": list(self.affected_step_ids),
            "risk_level": self.risk_level.value,
            "confidence_bp": self.confidence_bp,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_incomplete_plan_gap",
            "version": self.version,
            "gap_id": self.gap_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.gap_hash != self.compute_hash():
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "gap_hash does not match gap content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["gap_hash"] = self.gap_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        kind: MetacognitiveIncompletePlanGapKind,
        requirement: str,
        summary: str,
        affected_step_ids: Iterable[str] = (),
        risk_level: MetacognitiveRiskLevel,
        confidence_bp: int,
    ) -> "MetacognitiveIncompletePlanGap":
        normalized_kind = MetacognitiveIncompletePlanGapKind(kind)
        normalized_requirement = _text(requirement, "requirement")
        normalized_summary = _text(summary, "summary")
        normalized_steps = _identifiers(
            affected_step_ids,
            "affected_step_id",
            _STEP_ID,
        )
        normalized_risk = MetacognitiveRiskLevel(risk_level)
        normalized_confidence = _integer(
            confidence_bp,
            "confidence_bp",
            minimum=0,
            maximum=10_000,
        )
        identity = {
            "kind": normalized_kind.value,
            "requirement": normalized_requirement,
            "summary": normalized_summary,
            "affected_step_ids": list(normalized_steps),
            "risk_level": normalized_risk.value,
            "confidence_bp": normalized_confidence,
        }
        return cls(
            gap_id=f"metacognitive-plan-gap:{_sha256_document(identity)}",
            kind=normalized_kind,
            requirement=normalized_requirement,
            summary=normalized_summary,
            affected_step_ids=normalized_steps,
            risk_level=normalized_risk,
            confidence_bp=normalized_confidence,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveIncompletePlanGap":
        if data.get("record_type") != "metacognitive_incomplete_plan_gap":
            raise MetacognitiveIncompletePlanDetectionError(
                "record_type must be metacognitive_incomplete_plan_gap"
            )
        if "gap_hash" not in data:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "serialized gap is missing gap_hash"
            )
        return cls(
            gap_id=data["gap_id"],
            kind=data["kind"],
            requirement=data["requirement"],
            summary=data["summary"],
            affected_step_ids=tuple(data["affected_step_ids"]),
            risk_level=data["risk_level"],
            confidence_bp=data["confidence_bp"],
            gap_hash=data["gap_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveIncompletePlanGap":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan gap JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan gap JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveIncompletePlanDetectionResult:
    result_id: str
    request_json: str
    request_hash: str
    status: MetacognitiveIncompletePlanDetectionStatus
    gaps_json: tuple[str, ...]
    gap_hashes: tuple[str, ...]
    findings_json: tuple[str, ...]
    finding_hashes: tuple[str, ...]
    completed_at: str
    result_hash: str | None = None
    version: int = METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "result_id",
            _identifier(self.result_id, "result_id"),
        )
        request = MetacognitiveIncompletePlanDetectionRequest.from_json(
            _text(self.request_json, "request_json")
        )
        request.verify_hash()
        supplied_request_hash = _hash(self.request_hash, "request_hash")
        if supplied_request_hash != request.request_hash:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "request_hash does not match embedded request"
            )
        object.__setattr__(self, "request_json", request.to_json())
        object.__setattr__(self, "request_hash", supplied_request_hash)

        try:
            status = MetacognitiveIncompletePlanDetectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan detection status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        gaps = tuple(
            sorted(
                (
                    MetacognitiveIncompletePlanGap.from_json(
                        _text(payload, "gap_json")
                    )
                    for payload in self.gaps_json
                ),
                key=lambda item: item.gap_id,
            )
        )
        for gap in gaps:
            gap.verify_hash()
        actual_gap_hashes = tuple(
            sorted(gap.gap_hash or "" for gap in gaps)
        )
        supplied_gap_hashes = tuple(
            sorted(_hash(value, "gap_hash") for value in self.gap_hashes)
        )
        if supplied_gap_hashes != actual_gap_hashes:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "gap_hashes do not match embedded gaps"
            )
        object.__setattr__(
            self,
            "gaps_json",
            tuple(gap.to_json() for gap in gaps),
        )
        object.__setattr__(self, "gap_hashes", actual_gap_hashes)

        findings = tuple(
            sorted(
                (
                    MetacognitiveSupervisionFinding.from_json(
                        _text(payload, "finding_json")
                    )
                    for payload in self.findings_json
                ),
                key=lambda item: item.finding_id,
            )
        )
        context = request.context
        for finding in findings:
            finding.verify_hash()
            if (
                finding.context_id != context.context_id
                or finding.context_hash != context.context_hash
            ):
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "finding is not bound to request supervision context"
                )
        actual_finding_hashes = tuple(
            sorted(finding.finding_hash or "" for finding in findings)
        )
        supplied_finding_hashes = tuple(
            sorted(
                _hash(value, "finding_hash")
                for value in self.finding_hashes
            )
        )
        if supplied_finding_hashes != actual_finding_hashes:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "finding_hashes do not match embedded findings"
            )
        object.__setattr__(
            self,
            "findings_json",
            tuple(finding.to_json() for finding in findings),
        )
        object.__setattr__(self, "finding_hashes", actual_finding_hashes)

        if status is MetacognitiveIncompletePlanDetectionStatus.COMPLETE:
            if gaps or findings:
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "complete result cannot contain gaps or findings"
                )
        else:
            if not gaps or len(findings) != len(gaps):
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "incomplete result requires one finding per gap"
                )

        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at < request.requested_at:
            raise MetacognitiveIncompletePlanDetectionPolicyError(
                "detection completion cannot precede request time"
            )
        object.__setattr__(self, "completed_at", completed_at)

        if self.version != METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION:
            raise MetacognitiveIncompletePlanDetectionError(
                "unsupported incomplete-plan result format version"
            )
        expected_id = (
            f"metacognitive-incomplete-plan-result:{self.compute_identity_hash()}"
        )
        if self.result_id != expected_id:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "result_id does not match result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveIncompletePlanDetectionIntegrityError(
                    "result_hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def request(self) -> MetacognitiveIncompletePlanDetectionRequest:
        return MetacognitiveIncompletePlanDetectionRequest.from_json(
            self.request_json
        )

    @property
    def gaps(self) -> tuple[MetacognitiveIncompletePlanGap, ...]:
        return tuple(
            MetacognitiveIncompletePlanGap.from_json(payload)
            for payload in self.gaps_json
        )

    @property
    def findings(self) -> tuple[MetacognitiveSupervisionFinding, ...]:
        return tuple(
            MetacognitiveSupervisionFinding.from_json(payload)
            for payload in self.findings_json
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "status": self.status.value,
            "gap_hashes": list(self.gap_hashes),
            "finding_hashes": list(self.finding_hashes),
            "completed_at": self.completed_at,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_incomplete_plan_detection_result",
            "version": self.version,
            "result_id": self.result_id,
            "request_json": self.request_json,
            "gaps_json": list(self.gaps_json),
            "findings_json": list(self.findings_json),
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
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
        request: MetacognitiveIncompletePlanDetectionRequest,
        gaps: Iterable[MetacognitiveIncompletePlanGap],
        findings: Iterable[MetacognitiveSupervisionFinding],
        completed_at: str | datetime,
    ) -> "MetacognitiveIncompletePlanDetectionResult":
        if not isinstance(
            request,
            MetacognitiveIncompletePlanDetectionRequest,
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "request must be a MetacognitiveIncompletePlanDetectionRequest"
            )
        request.verify_hash()
        normalized_gaps = tuple(sorted(gaps, key=lambda item: item.gap_id))
        if not all(
            isinstance(item, MetacognitiveIncompletePlanGap)
            for item in normalized_gaps
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "gaps must contain MetacognitiveIncompletePlanGap values"
            )
        for gap in normalized_gaps:
            gap.verify_hash()
        normalized_findings = tuple(
            sorted(findings, key=lambda item: item.finding_id)
        )
        if not all(
            isinstance(item, MetacognitiveSupervisionFinding)
            for item in normalized_findings
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "findings must contain MetacognitiveSupervisionFinding values"
            )
        for finding in normalized_findings:
            finding.verify_hash()

        status = (
            MetacognitiveIncompletePlanDetectionStatus.INCOMPLETE
            if normalized_gaps
            else MetacognitiveIncompletePlanDetectionStatus.COMPLETE
        )
        completed = _utc_timestamp(completed_at, "completed_at")
        identity = {
            "request_hash": request.request_hash,
            "status": status.value,
            "gap_hashes": sorted(
                gap.gap_hash or "" for gap in normalized_gaps
            ),
            "finding_hashes": sorted(
                finding.finding_hash or ""
                for finding in normalized_findings
            ),
            "completed_at": completed,
        }
        return cls(
            result_id=(
                "metacognitive-incomplete-plan-result:"
                f"{_sha256_document(identity)}"
            ),
            request_json=request.to_json(),
            request_hash=request.request_hash or "",
            status=status,
            gaps_json=tuple(gap.to_json() for gap in normalized_gaps),
            gap_hashes=tuple(
                gap.gap_hash or "" for gap in normalized_gaps
            ),
            findings_json=tuple(
                finding.to_json() for finding in normalized_findings
            ),
            finding_hashes=tuple(
                finding.finding_hash or ""
                for finding in normalized_findings
            ),
            completed_at=completed,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveIncompletePlanDetectionResult":
        if (
            data.get("record_type")
            != "metacognitive_incomplete_plan_detection_result"
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "record_type must be metacognitive_incomplete_plan_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveIncompletePlanDetectionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            result_id=data["result_id"],
            request_json=data["request_json"],
            request_hash=data["request_hash"],
            status=data["status"],
            gaps_json=tuple(data["gaps_json"]),
            gap_hashes=tuple(data["gap_hashes"]),
            findings_json=tuple(data["findings_json"]),
            finding_hashes=tuple(data["finding_hashes"]),
            completed_at=data["completed_at"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveIncompletePlanDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveIncompletePlanDetectionError(
                "incomplete-plan result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(slots=True)
class MetacognitiveIncompletePlanDetector:
    """Read-only deterministic detector for declared plan-completeness gaps."""

    def detect(
        self,
        *,
        request: MetacognitiveIncompletePlanDetectionRequest,
        completed_at: str | datetime,
    ) -> MetacognitiveIncompletePlanDetectionResult:
        if not isinstance(
            request,
            MetacognitiveIncompletePlanDetectionRequest,
        ):
            raise MetacognitiveIncompletePlanDetectionError(
                "request must be a MetacognitiveIncompletePlanDetectionRequest"
            )
        request.verify_hash()
        policy = request.policy
        context = request.context
        plan = request.plan
        gaps: list[MetacognitiveIncompletePlanGap] = []

        if len(plan.steps) < policy.minimum_step_count:
            gaps.append(
                MetacognitiveIncompletePlanGap.capture(
                    kind=MetacognitiveIncompletePlanGapKind.MINIMUM_STEP_COUNT,
                    requirement=f"minimum-step-count:{policy.minimum_step_count}",
                    summary=(
                        "Execution plan contains fewer steps than the declared "
                        f"minimum ({len(plan.steps)} < {policy.minimum_step_count})."
                    ),
                    risk_level=MetacognitiveRiskLevel.HIGH,
                    confidence_bp=policy.finding_confidence_bp,
                )
            )

        actual_step_ids = {step.step_id for step in plan.steps}
        for step_id in policy.required_step_ids:
            if step_id not in actual_step_ids:
                gaps.append(
                    MetacognitiveIncompletePlanGap.capture(
                        kind=MetacognitiveIncompletePlanGapKind.MISSING_REQUIRED_STEP,
                        requirement=f"required-step:{step_id}",
                        summary=(
                            "Execution plan is missing declared required step "
                            f"{step_id}."
                        ),
                        risk_level=MetacognitiveRiskLevel.HIGH,
                        confidence_bp=policy.finding_confidence_bp,
                    )
                )

        actual_capability_ids = {
            step.capability_id for step in plan.steps
        }
        for capability_id in policy.required_capability_ids:
            if capability_id not in actual_capability_ids:
                gaps.append(
                    MetacognitiveIncompletePlanGap.capture(
                        kind=(
                            MetacognitiveIncompletePlanGapKind.MISSING_REQUIRED_CAPABILITY
                        ),
                        requirement=f"required-capability:{capability_id}",
                        summary=(
                            "Execution plan does not cover declared required "
                            f"capability {capability_id}."
                        ),
                        risk_level=MetacognitiveRiskLevel.HIGH,
                        confidence_bp=policy.finding_confidence_bp,
                    )
                )

        if policy.require_bound_agents:
            for step in plan.steps:
                if step.assigned_agent_id is None:
                    gaps.append(
                        MetacognitiveIncompletePlanGap.capture(
                            kind=MetacognitiveIncompletePlanGapKind.UNBOUND_STEP,
                            requirement=f"bound-agent-for-step:{step.step_id}",
                            summary=(
                                "Execution step has no assigned agent: "
                                f"{step.step_id}."
                            ),
                            affected_step_ids=(step.step_id,),
                            risk_level=MetacognitiveRiskLevel.MEDIUM,
                            confidence_bp=policy.finding_confidence_bp,
                        )
                    )

        if policy.require_approval_trace:
            if (
                plan.requires_human_approval
                and plan.approval_reference is None
            ):
                gaps.append(
                    MetacognitiveIncompletePlanGap.capture(
                        kind=(
                            MetacognitiveIncompletePlanGapKind.MISSING_PLAN_APPROVAL
                        ),
                        requirement="plan-approval-trace",
                        summary=(
                            "Execution plan requires human approval but has "
                            "no approval reference."
                        ),
                        risk_level=MetacognitiveRiskLevel.HIGH,
                        confidence_bp=policy.finding_confidence_bp,
                    )
                )
            for step in plan.steps:
                if not step.requires_human_approval:
                    continue
                effective_approval = (
                    step.approval_reference
                    or plan.approval_reference
                )
                if effective_approval is None:
                    gaps.append(
                        MetacognitiveIncompletePlanGap.capture(
                            kind=(
                                MetacognitiveIncompletePlanGapKind.MISSING_STEP_APPROVAL
                            ),
                            requirement=f"approval-trace-for-step:{step.step_id}",
                            summary=(
                                "Execution step requires human approval but "
                                f"has no effective approval trace: {step.step_id}."
                            ),
                            affected_step_ids=(step.step_id,),
                            risk_level=MetacognitiveRiskLevel.HIGH,
                            confidence_bp=policy.finding_confidence_bp,
                        )
                    )

        gaps = sorted(
            gaps,
            key=lambda item: (
                item.kind.value,
                item.requirement,
                item.gap_id,
            ),
        )
        findings = tuple(
            MetacognitiveSupervisionFinding.from_context(
                context=context,
                kind=MetacognitiveFindingKind.EVIDENCE_GAP,
                risk_level=gap.risk_level,
                summary=gap.summary,
                evidence_references=(request.plan_evidence_reference,),
                affected_step_ids=gap.affected_step_ids,
                confidence_bp=gap.confidence_bp,
            )
            for gap in gaps
        )
        return MetacognitiveIncompletePlanDetectionResult.capture(
            request=request,
            gaps=gaps,
            findings=findings,
            completed_at=completed_at,
        )
