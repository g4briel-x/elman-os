"""Deterministic missing-dependency detection for ELMAN-OS v0.7.

The detector evaluates an already-valid ``ExecutionPlan`` against an explicit
set of required dependency relations. It does not duplicate structural graph
validation already enforced by ``ExecutionPlan`` and never infers dependencies
from natural language.
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
    MetacognitiveSupervisionFinding,
)

METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION: Final[int] = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetacognitiveMissingDependencyDetectionError(ValueError):
    """A missing-dependency detection contract or operation is invalid."""


class MetacognitiveMissingDependencyDetectionIntegrityError(
    MetacognitiveMissingDependencyDetectionError
):
    """A serialized contract or binding fails deterministic integrity checks."""


class MetacognitiveMissingDependencyDetectionPolicyError(
    MetacognitiveMissingDependencyDetectionError
):
    """A configured dependency requirement is unsafe or inconsistent."""


class MetacognitiveMissingDependencyDetectionStatus(StrEnum):
    SATISFIED = "satisfied"
    MISSING = "missing"


class MetacognitiveDependencyRelation(StrEnum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetacognitiveMissingDependencyDetectionError(
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
        raise MetacognitiveMissingDependencyDetectionError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise MetacognitiveMissingDependencyDetectionError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MetacognitiveMissingDependencyDetectionError(
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
        raise MetacognitiveMissingDependencyDetectionError(
            f"{name} must be an integer between 0 and 10000"
        )
    return value



def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise MetacognitiveMissingDependencyDetectionError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise MetacognitiveMissingDependencyDetectionError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise MetacognitiveMissingDependencyDetectionError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise MetacognitiveMissingDependencyDetectionError(
                f"{name} must be UTC"
            )
    else:
        raise MetacognitiveMissingDependencyDetectionError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _plan_state_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetacognitiveRequiredDependency:
    dependent_step_id: str
    prerequisite_step_id: str
    relation: MetacognitiveDependencyRelation = MetacognitiveDependencyRelation.DIRECT
    version: int = METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dependent_step_id",
            _identifier(self.dependent_step_id, "dependent_step_id", _STEP_ID),
        )
        object.__setattr__(
            self,
            "prerequisite_step_id",
            _identifier(self.prerequisite_step_id, "prerequisite_step_id", _STEP_ID),
        )
        if self.dependent_step_id == self.prerequisite_step_id:
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "a required dependency cannot target the same step"
            )
        try:
            relation = MetacognitiveDependencyRelation(self.relation)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "required dependency relation is invalid"
            ) from exc
        object.__setattr__(self, "relation", relation)
        if self.version != METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION:
            raise MetacognitiveMissingDependencyDetectionError(
                "unsupported required dependency format version"
            )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            self.dependent_step_id,
            self.prerequisite_step_id,
            self.relation.value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_required_dependency",
            "version": self.version,
            "dependent_step_id": self.dependent_step_id,
            "prerequisite_step_id": self.prerequisite_step_id,
            "relation": self.relation.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetacognitiveRequiredDependency":
        if data.get("record_type") != "metacognitive_required_dependency":
            raise MetacognitiveMissingDependencyDetectionError(
                "record_type must be metacognitive_required_dependency"
            )
        return cls(
            dependent_step_id=data["dependent_step_id"],
            prerequisite_step_id=data["prerequisite_step_id"],
            relation=data["relation"],
            version=data.get("version", 0),
        )


@dataclass(frozen=True, slots=True)
class MetacognitiveMissingDependencyDetectionPolicy:
    policy_id: str
    required_dependencies: tuple[MetacognitiveRequiredDependency, ...]
    finding_confidence_bp: int = 9500
    fail_closed: bool = True
    version: int = METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        dependencies = tuple(self.required_dependencies)
        if not dependencies:
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "required_dependencies must contain at least one dependency"
            )
        if not all(
            isinstance(item, MetacognitiveRequiredDependency) for item in dependencies
        ):
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "required_dependencies must contain MetacognitiveRequiredDependency values"
            )
        identities = [item.identity for item in dependencies]
        if len(identities) != len(set(identities)):
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "required_dependencies cannot contain duplicates"
            )
        object.__setattr__(
            self,
            "required_dependencies",
            tuple(sorted(dependencies, key=lambda item: item.identity)),
        )
        object.__setattr__(
            self,
            "finding_confidence_bp",
            _basis_points(self.finding_confidence_bp, "finding_confidence_bp"),
        )
        object.__setattr__(self, "fail_closed", _boolean(self.fail_closed, "fail_closed"))
        if not self.fail_closed:
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "missing-dependency detection must fail closed"
            )
        if self.version != METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION:
            raise MetacognitiveMissingDependencyDetectionError(
                "unsupported missing-dependency detection policy format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_missing_dependency_detection_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "required_dependencies": [item.to_dict() for item in self.required_dependencies],
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
    ) -> "MetacognitiveMissingDependencyDetectionPolicy":
        if data.get("record_type") != "metacognitive_missing_dependency_detection_policy":
            raise MetacognitiveMissingDependencyDetectionError(
                "record_type must be metacognitive_missing_dependency_detection_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            required_dependencies=tuple(
                MetacognitiveRequiredDependency.from_dict(item)
                for item in data["required_dependencies"]
            ),
            finding_confidence_bp=data["finding_confidence_bp"],
            fail_closed=data["fail_closed"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveMissingDependencyDetectionPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveMissingDependencyDetectionRequest:
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
    version: int = METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "policy_hash", _hash(self.policy_hash, "policy_hash"))
        object.__setattr__(self, "context_hash", _hash(self.context_hash, "context_hash"))
        object.__setattr__(
            self,
            "plan_state_hash",
            _hash(self.plan_state_hash, "plan_state_hash"),
        )
        object.__setattr__(
            self,
            "plan_evidence_reference",
            _identifier(self.plan_evidence_reference, "plan_evidence_reference"),
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        object.__setattr__(self, "requested_at", _utc_timestamp(self.requested_at, "requested_at"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION:
            raise MetacognitiveMissingDependencyDetectionError(
                "unsupported missing-dependency request format version"
            )
        policy = self.policy
        context = self.context
        plan = self.plan
        if policy.policy_hash != self.policy_hash:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "policy_hash does not match policy_json"
            )
        context.verify_hash()
        if context.context_hash != self.context_hash:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "context_hash does not match context_json"
            )
        computed_plan_hash = _plan_state_hash(plan)
        if computed_plan_hash != self.plan_state_hash:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "plan_state_hash does not match plan_json"
            )
        if context.plan_id != plan.plan_id or context.project_id != plan.project_id:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "supervision context does not identify the supplied execution plan"
            )
        if context.plan_state_hash != self.plan_state_hash:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "supervision context is not bound to the supplied plan state"
            )
        if self.plan_evidence_reference not in context.evidence_references:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "plan evidence reference is not bound to the supervision context"
            )
        expected_id = f"missing-dependency-request:{self.compute_identity_hash()}"
        if self.request_id != expected_id:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "request_id does not match request identity"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise MetacognitiveMissingDependencyDetectionIntegrityError(
                    "request_hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> MetacognitiveMissingDependencyDetectionPolicy:
        return MetacognitiveMissingDependencyDetectionPolicy.from_json(self.policy_json)

    @property
    def context(self) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.from_json(self.context_json)

    @property
    def plan(self) -> ExecutionPlan:
        try:
            return ExecutionPlan.from_json(self.plan_json)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "plan_json is not a valid execution plan"
            ) from exc

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
            "record_type": "metacognitive_missing_dependency_detection_request",
            "version": self.version,
            "request_id": self.request_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "context_json": self.context_json,
            "context_hash": self.context_hash,
            "plan_json": self.plan_json,
            "plan_state_hash": self.plan_state_hash,
            "plan_evidence_reference": self.plan_evidence_reference,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
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
        policy: MetacognitiveMissingDependencyDetectionPolicy,
        context: MetacognitiveSupervisionContext,
        plan: ExecutionPlan,
        plan_evidence_reference: str,
        requested_by: str,
        requested_at: str,
        reason: str,
    ) -> "MetacognitiveMissingDependencyDetectionRequest":
        if not isinstance(policy, MetacognitiveMissingDependencyDetectionPolicy):
            raise MetacognitiveMissingDependencyDetectionError(
                "policy must be a MetacognitiveMissingDependencyDetectionPolicy"
            )
        if not isinstance(context, MetacognitiveSupervisionContext):
            raise MetacognitiveMissingDependencyDetectionError(
                "context must be a MetacognitiveSupervisionContext"
            )
        if not isinstance(plan, ExecutionPlan):
            raise MetacognitiveMissingDependencyDetectionError(
                "plan must be an ExecutionPlan"
            )
        context.verify_hash()
        plan_hash = _plan_state_hash(plan)
        evidence = _identifier(plan_evidence_reference, "plan_evidence_reference")
        requester = _identifier(requested_by, "requested_by", _AGENT_ID)
        timestamp = _utc_timestamp(requested_at, "requested_at")
        normalized_reason = _text(reason, "reason")
        if context.plan_id != plan.plan_id or context.project_id != plan.project_id:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "supervision context does not identify the supplied execution plan"
            )
        if context.plan_state_hash != plan_hash:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "supervision context is not bound to the supplied plan state"
            )
        if evidence not in context.evidence_references:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "plan evidence reference is not bound to the supervision context"
            )
        identity = {
            "policy_hash": policy.policy_hash,
            "context_hash": context.context_hash,
            "plan_state_hash": plan_hash,
            "plan_evidence_reference": evidence,
            "requested_by": requester,
            "requested_at": timestamp,
            "reason": normalized_reason,
        }
        return cls(
            request_id=f"missing-dependency-request:{_sha256_document(identity)}",
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            context_json=context.to_json(),
            context_hash=context.context_hash or "",
            plan_json=plan.to_json(),
            plan_state_hash=plan_hash,
            plan_evidence_reference=evidence,
            requested_by=requester,
            requested_at=timestamp,
            reason=normalized_reason,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveMissingDependencyDetectionRequest":
        if data.get("record_type") != "metacognitive_missing_dependency_detection_request":
            raise MetacognitiveMissingDependencyDetectionError(
                "record_type must be metacognitive_missing_dependency_detection_request"
            )
        if "request_hash" not in data:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
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
    ) -> "MetacognitiveMissingDependencyDetectionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class MetacognitiveMissingDependencyGap:
    gap_id: str
    dependent_step_id: str
    prerequisite_step_id: str
    relation: MetacognitiveDependencyRelation
    evidence_reference: str
    gap_hash: str | None = None
    version: int = METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "gap_id", _identifier(self.gap_id, "gap_id"))
        object.__setattr__(
            self,
            "dependent_step_id",
            _identifier(self.dependent_step_id, "dependent_step_id", _STEP_ID),
        )
        object.__setattr__(
            self,
            "prerequisite_step_id",
            _identifier(self.prerequisite_step_id, "prerequisite_step_id", _STEP_ID),
        )
        try:
            relation = MetacognitiveDependencyRelation(self.relation)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                "missing dependency relation is invalid"
            ) from exc
        object.__setattr__(self, "relation", relation)
        object.__setattr__(
            self,
            "evidence_reference",
            _identifier(self.evidence_reference, "evidence_reference"),
        )
        if self.version != METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION:
            raise MetacognitiveMissingDependencyDetectionError(
                "unsupported missing dependency gap format version"
            )
        expected_id = f"missing-dependency-gap:{self.compute_identity_hash()}"
        if self.gap_id != expected_id:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "gap_id does not match gap identity"
            )
        computed = self.compute_hash()
        if self.gap_hash is None:
            object.__setattr__(self, "gap_hash", computed)
        else:
            supplied = _hash(self.gap_hash, "gap_hash")
            if supplied != computed:
                raise MetacognitiveMissingDependencyDetectionIntegrityError(
                    "gap_hash does not match gap content"
                )
            object.__setattr__(self, "gap_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "dependent_step_id": self.dependent_step_id,
            "prerequisite_step_id": self.prerequisite_step_id,
            "relation": self.relation.value,
            "evidence_reference": self.evidence_reference,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_missing_dependency_gap",
            "version": self.version,
            "gap_id": self.gap_id,
            **self.identity_material(),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.gap_hash != self.compute_hash():
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
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
        dependent_step_id: str,
        prerequisite_step_id: str,
        relation: MetacognitiveDependencyRelation | str,
        evidence_reference: str,
    ) -> "MetacognitiveMissingDependencyGap":
        dependent = _identifier(dependent_step_id, "dependent_step_id", _STEP_ID)
        prerequisite = _identifier(prerequisite_step_id, "prerequisite_step_id", _STEP_ID)
        normalized_relation = MetacognitiveDependencyRelation(relation)
        evidence = _identifier(evidence_reference, "evidence_reference")
        identity = {
            "dependent_step_id": dependent,
            "prerequisite_step_id": prerequisite,
            "relation": normalized_relation.value,
            "evidence_reference": evidence,
        }
        return cls(
            gap_id=f"missing-dependency-gap:{_sha256_document(identity)}",
            dependent_step_id=dependent,
            prerequisite_step_id=prerequisite,
            relation=normalized_relation,
            evidence_reference=evidence,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "MetacognitiveMissingDependencyGap":
        if data.get("record_type") != "metacognitive_missing_dependency_gap":
            raise MetacognitiveMissingDependencyDetectionError(
                "record_type must be metacognitive_missing_dependency_gap"
            )
        if "gap_hash" not in data:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "serialized gap is missing gap_hash"
            )
        return cls(
            gap_id=data["gap_id"],
            dependent_step_id=data["dependent_step_id"],
            prerequisite_step_id=data["prerequisite_step_id"],
            relation=data["relation"],
            evidence_reference=data["evidence_reference"],
            gap_hash=data["gap_hash"],
            version=data.get("version", 0),
        )


@dataclass(frozen=True, slots=True)
class MetacognitiveMissingDependencyDetectionResult:
    result_id: str
    request_id: str
    request_hash: str
    status: MetacognitiveMissingDependencyDetectionStatus
    gaps: tuple[MetacognitiveMissingDependencyGap, ...]
    findings: tuple[MetacognitiveSupervisionFinding, ...]
    result_hash: str | None = None
    version: int = METACOGNITIVE_MISSING_DEPENDENCY_DETECTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identifier(self.result_id, "result_id"))
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "request_hash", _hash(self.request_hash, "request_hash"))
        try:
            status = MetacognitiveMissingDependencyDetectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency result status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        gaps = tuple(self.gaps)
        findings = tuple(self.findings)
        if not all(isinstance(gap, MetacognitiveMissingDependencyGap) for gap in gaps):
            raise MetacognitiveMissingDependencyDetectionError(
                "gaps must contain MetacognitiveMissingDependencyGap values"
            )
        if not all(isinstance(item, MetacognitiveSupervisionFinding) for item in findings):
            raise MetacognitiveMissingDependencyDetectionError(
                "findings must contain MetacognitiveSupervisionFinding values"
            )
        for gap in gaps:
            gap.verify_hash()
        for finding in findings:
            finding.verify_hash()
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "findings", findings)
        expected_status = (
            MetacognitiveMissingDependencyDetectionStatus.MISSING
            if gaps
            else MetacognitiveMissingDependencyDetectionStatus.SATISFIED
        )
        if status is not expected_status:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "result status does not match detected gaps"
            )
        if len(gaps) != len(findings):
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "result must contain exactly one finding per missing dependency"
            )
        expected_id = f"missing-dependency-result:{self.compute_identity_hash()}"
        if self.result_id != expected_id:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "result_id does not match result identity"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise MetacognitiveMissingDependencyDetectionIntegrityError(
                    "result_hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def identity_material(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "gap_hashes": [gap.gap_hash for gap in self.gaps],
            "finding_hashes": [finding.finding_hash for finding in self.findings],
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "metacognitive_missing_dependency_detection_result",
            "version": self.version,
            "result_id": self.result_id,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
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
        request: MetacognitiveMissingDependencyDetectionRequest,
        gaps: Iterable[MetacognitiveMissingDependencyGap],
        findings: Iterable[MetacognitiveSupervisionFinding],
    ) -> "MetacognitiveMissingDependencyDetectionResult":
        request.verify_hash()
        normalized_gaps = tuple(gaps)
        normalized_findings = tuple(findings)
        status = (
            MetacognitiveMissingDependencyDetectionStatus.MISSING
            if normalized_gaps
            else MetacognitiveMissingDependencyDetectionStatus.SATISFIED
        )
        identity = {
            "request_id": request.request_id,
            "request_hash": request.request_hash,
            "status": status.value,
            "gap_hashes": [gap.gap_hash for gap in normalized_gaps],
            "finding_hashes": [finding.finding_hash for finding in normalized_findings],
        }
        return cls(
            result_id=f"missing-dependency-result:{_sha256_document(identity)}",
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
    ) -> "MetacognitiveMissingDependencyDetectionResult":
        if data.get("record_type") != "metacognitive_missing_dependency_detection_result":
            raise MetacognitiveMissingDependencyDetectionError(
                "record_type must be metacognitive_missing_dependency_detection_result"
            )
        if "result_hash" not in data:
            raise MetacognitiveMissingDependencyDetectionIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            result_id=data["result_id"],
            request_id=data["request_id"],
            request_hash=data["request_hash"],
            status=data["status"],
            gaps=tuple(MetacognitiveMissingDependencyGap.from_dict(item) for item in data["gaps"]),
            findings=tuple(MetacognitiveSupervisionFinding.from_dict(item) for item in data["findings"]),
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "MetacognitiveMissingDependencyDetectionResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise MetacognitiveMissingDependencyDetectionError(
                "missing-dependency result JSON must be an object"
            )
        return cls.from_dict(data)


def _transitive_prerequisites(plan: ExecutionPlan) -> dict[str, frozenset[str]]:
    by_id = {step.step_id: step for step in plan.steps}
    cache: dict[str, frozenset[str]] = {}

    def visit(step_id: str) -> frozenset[str]:
        cached = cache.get(step_id)
        if cached is not None:
            return cached
        dependencies: set[str] = set()
        for dependency in by_id[step_id].dependencies:
            dependencies.add(dependency)
            dependencies.update(visit(dependency))
        result = frozenset(dependencies)
        cache[step_id] = result
        return result

    for step_id in by_id:
        visit(step_id)
    return cache


class MetacognitiveMissingDependencyDetector:
    """Read-only deterministic detector for explicitly required dependency edges."""

    def detect(
        self,
        request: MetacognitiveMissingDependencyDetectionRequest,
    ) -> MetacognitiveMissingDependencyDetectionResult:
        if not isinstance(request, MetacognitiveMissingDependencyDetectionRequest):
            raise MetacognitiveMissingDependencyDetectionError(
                "request must be a MetacognitiveMissingDependencyDetectionRequest"
            )
        request.verify_hash()
        policy = request.policy
        context = request.context
        plan = request.plan
        by_id = {step.step_id: step for step in plan.steps}

        referenced = {
            step_id
            for requirement in policy.required_dependencies
            for step_id in (
                requirement.dependent_step_id,
                requirement.prerequisite_step_id,
            )
        }
        missing_steps = sorted(referenced - set(by_id))
        if missing_steps:
            raise MetacognitiveMissingDependencyDetectionPolicyError(
                "dependency policy references steps absent from the execution plan: "
                + ", ".join(missing_steps)
            )

        transitive = _transitive_prerequisites(plan)
        gaps: list[MetacognitiveMissingDependencyGap] = []
        findings: list[MetacognitiveSupervisionFinding] = []
        for requirement in policy.required_dependencies:
            dependent = by_id[requirement.dependent_step_id]
            if requirement.relation is MetacognitiveDependencyRelation.DIRECT:
                satisfied = requirement.prerequisite_step_id in dependent.dependencies
            else:
                satisfied = (
                    requirement.prerequisite_step_id
                    in transitive[requirement.dependent_step_id]
                )
            if satisfied:
                continue

            gap = MetacognitiveMissingDependencyGap.capture(
                dependent_step_id=requirement.dependent_step_id,
                prerequisite_step_id=requirement.prerequisite_step_id,
                relation=requirement.relation,
                evidence_reference=request.plan_evidence_reference,
            )
            gaps.append(gap)
            summary = (
                f"Required {requirement.relation.value} dependency from "
                f"{requirement.dependent_step_id} to prerequisite "
                f"{requirement.prerequisite_step_id} is missing."
            )
            findings.append(
                MetacognitiveSupervisionFinding.from_context(
                    context=context,
                    kind=MetacognitiveFindingKind.EVIDENCE_GAP,
                    risk_level=MetacognitiveRiskLevel.HIGH,
                    summary=summary,
                    evidence_references=(request.plan_evidence_reference,),
                    affected_step_ids=(
                        requirement.dependent_step_id,
                        requirement.prerequisite_step_id,
                    ),
                    confidence_bp=policy.finding_confidence_bp,
                )
            )

        return MetacognitiveMissingDependencyDetectionResult.capture(
            request=request,
            gaps=gaps,
            findings=findings,
        )


def detect_missing_dependencies(
    request: MetacognitiveMissingDependencyDetectionRequest,
) -> MetacognitiveMissingDependencyDetectionResult:
    """Detect missing explicit dependency requirements without mutating state."""

    return MetacognitiveMissingDependencyDetector().detect(request)
