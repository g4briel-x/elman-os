"""Controlled resume authorization for one restored selected orchestration state.

This boundary consumes a verified selected-state restoration result and an
explicit human approval record, recomputes the checkpoint resume assessment,
and delegates the declarative authorization decision to the existing
``execution_resume`` contract.

It never applies the resulting command, mutates the plan or journal, writes a
checkpoint, touches persisted state, executes an agent, performs network
access, or invokes an AI provider.
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
from .artifact_orchestration_selected_state_restoration import (
    ArtifactOrchestrationSelectedStateRestorationResult,
    ArtifactOrchestrationSelectedStateRestorationStatus,
)
from .execution_checkpoint import (
    ExecutionCheckpointError,
    ResumeAssessment,
)
from .execution_resume import (
    ExecutionResumeError,
    ResumeCommand,
    ResumeDecision,
    ResumeDecisionStatus,
    ResumePolicy,
    ResumeRequest,
    decide_resume,
)


ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{2,191}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactOrchestrationSelectedStateResumeAuthorizationError(RuntimeError):
    """A selected-state resume authorization contract is invalid."""


class ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError(
    ArtifactOrchestrationSelectedStateResumeAuthorizationError
):
    """Explicit human approval evidence is absent or outside its scope."""


class ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
    ArtifactOrchestrationSelectedStateResumeAuthorizationError
):
    """A cryptographic or cross-boundary binding is invalid."""


class ArtifactOrchestrationSelectedStateResumeAuthorizationExecutionError(
    ArtifactOrchestrationSelectedStateResumeAuthorizationError
):
    """The delegated declarative resume decision failed."""


class ArtifactOrchestrationSelectedStateResumeAuthorizationStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _payload_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
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
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )

    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _step_ids(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be a tuple or list"
        )
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must be a tuple or list"
        ) from exc
    normalized = tuple(
        sorted(
            {
                _identifier(item, "step_id", _STEP_ID)
                for item in items
            }
        )
    )
    if not normalized:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
            f"{name} must contain at least one step"
        )
    return normalized


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _assessment_from_json(payload: str) -> ResumeAssessment:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
            "resume assessment JSON is invalid"
        ) from exc
    if not isinstance(data, dict):
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
            "resume assessment JSON must be an object"
        )
    try:
        return ResumeAssessment(
            checkpoint_id=data["checkpoint_id"],
            status=data["status"],
            can_resume=data["can_resume"],
            reasons=tuple(data["reasons"]),
            ready_step_ids=tuple(data["ready_step_ids"]),
            running_step_ids=tuple(data["running_step_ids"]),
            current_event_count=data["current_event_count"],
            current_head_hash=data["current_head_hash"],
        )
    except (ExecutionCheckpointError, KeyError, TypeError, ValueError) as exc:
        raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
            "resume assessment JSON cannot be reconstructed"
        ) from exc


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationHumanResumeApproval:
    approval_id: str
    approval_reference: str
    approved_by: str
    approved_at: str
    statement: str
    approved_step_ids: tuple[str, ...]
    restoration_result_hash: str
    approval_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "approval_id",
            _identifier(self.approval_id, "approval_id"),
        )
        object.__setattr__(
            self,
            "approval_reference",
            _identifier(self.approval_reference, "approval_reference"),
        )
        object.__setattr__(
            self,
            "approved_by",
            _identifier(self.approved_by, "approved_by", _PRINCIPAL),
        )
        object.__setattr__(
            self,
            "approved_at",
            _utc_timestamp(self.approved_at, "approved_at"),
        )
        object.__setattr__(
            self,
            "statement",
            _text(self.statement, "statement"),
        )
        object.__setattr__(
            self,
            "approved_step_ids",
            _step_ids(self.approved_step_ids, "approved_step_ids"),
        )
        object.__setattr__(
            self,
            "restoration_result_hash",
            _hash(
                self.restoration_result_hash,
                "restoration_result_hash",
            ),
        )
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "unsupported resume authorization format version"
            )

        computed = self.compute_hash()
        if self.approval_hash is None:
            object.__setattr__(self, "approval_hash", computed)
        else:
            supplied = _hash(self.approval_hash, "approval_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    "approval hash does not match approval content"
                )
            object.__setattr__(self, "approval_hash", supplied)

    @classmethod
    def for_restoration(
        cls,
        restoration_result: ArtifactOrchestrationSelectedStateRestorationResult,
        *,
        approval_reference: str,
        approved_by: str,
        approved_at: str | datetime,
        statement: str,
        approved_step_ids: tuple[str, ...] | list[str],
        approval_id: str | None = None,
    ) -> "ArtifactOrchestrationHumanResumeApproval":
        if not isinstance(
            restoration_result,
            ArtifactOrchestrationSelectedStateRestorationResult,
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "restoration_result must be an "
                "ArtifactOrchestrationSelectedStateRestorationResult"
            )
        restoration_result.verify_hash()
        result_hash = restoration_result.result_hash
        assert result_hash is not None
        normalized_reference = _identifier(
            approval_reference,
            "approval_reference",
        )
        normalized_principal = _identifier(
            approved_by,
            "approved_by",
            _PRINCIPAL,
        )
        normalized_at = _utc_timestamp(approved_at, "approved_at")
        normalized_statement = _text(statement, "statement")
        normalized_steps = _step_ids(
            approved_step_ids,
            "approved_step_ids",
        )
        identity_hash = _sha256_document(
            {
                "record_type": "artifact_orchestration_human_resume_approval_identity",
                "restoration_result_hash": result_hash,
                "approval_reference": normalized_reference,
                "approved_by": normalized_principal,
                "approved_at": normalized_at,
                "statement": normalized_statement,
                "approved_step_ids": list(normalized_steps),
            }
        )
        return cls(
            approval_id=(
                approval_id
                if approval_id is not None
                else f"resume-approval:{identity_hash}"
            ),
            approval_reference=normalized_reference,
            approved_by=normalized_principal,
            approved_at=normalized_at,
            statement=normalized_statement,
            approved_step_ids=normalized_steps,
            restoration_result_hash=result_hash,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_orchestration_human_resume_approval",
            "version": self.version,
            "approval_id": self.approval_id,
            "approval_reference": self.approval_reference,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "statement": self.statement,
            "approved_step_ids": list(self.approved_step_ids),
            "restoration_result_hash": self.restoration_result_hash,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.approval_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "approval hash does not match approval content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["approval_hash"] = self.approval_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationHumanResumeApproval":
        if data.get("record_type") != (
            "artifact_orchestration_human_resume_approval"
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "record_type must be "
                "artifact_orchestration_human_resume_approval"
            )
        if "approval_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "serialized approval is missing approval_hash"
            )
        return cls(
            approval_id=data["approval_id"],
            approval_reference=data["approval_reference"],
            approved_by=data["approved_by"],
            approved_at=data["approved_at"],
            statement=data["statement"],
            approved_step_ids=tuple(data["approved_step_ids"]),
            restoration_result_hash=data["restoration_result_hash"],
            approval_hash=data["approval_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationHumanResumeApproval":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "human resume approval JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "human resume approval JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy:
    policy_id: str
    resume_policy: ResumePolicy
    require_recomputed_assessment_match: bool = True
    require_explicit_step_scope: bool = True
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        if not isinstance(self.resume_policy, ResumePolicy):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume_policy must be a ResumePolicy"
            )
        for field_name in (
            "require_recomputed_assessment_match",
            "require_explicit_step_scope",
        ):
            value = _boolean(getattr(self, field_name), field_name)
            if value is not True:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                    f"{field_name} must remain enabled"
                )
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "unsupported resume authorization format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_authorization_policy"
            ),
            "version": self.version,
            "policy_id": self.policy_id,
            "resume_policy": self.resume_policy.to_dict(),
            "require_recomputed_assessment_match": (
                self.require_recomputed_assessment_match
            ),
            "require_explicit_step_scope": self.require_explicit_step_scope,
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
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_authorization_policy"
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_authorization_policy"
            )
        raw_policy = data.get("resume_policy")
        if not isinstance(raw_policy, Mapping):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume_policy must be an object"
            )
        return cls(
            policy_id=data["policy_id"],
            resume_policy=ResumePolicy.from_dict(raw_policy),
            require_recomputed_assessment_match=data[
                "require_recomputed_assessment_match"
            ],
            require_explicit_step_scope=data[
                "require_explicit_step_scope"
            ],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeAuthorizationRequest:
    authorization_id: str
    policy_id: str
    policy_hash: str
    policy_json: str
    restoration_result_hash: str
    restoration_result_json: str
    approval_hash: str
    approval_json: str
    requested_by: str
    requested_at: str
    rationale: str
    requested_step_ids: tuple[str, ...]
    request_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorization_id",
            _identifier(self.authorization_id, "authorization_id"),
        )
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "policy_hash",
            _hash(self.policy_hash, "policy_hash"),
        )
        policy = ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy.from_json(
            _payload_text(self.policy_json, "policy_json")
        )
        if policy.policy_id != self.policy_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "policy_id does not match embedded policy"
            )
        if policy.policy_hash != self.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())

        object.__setattr__(
            self,
            "restoration_result_hash",
            _hash(
                self.restoration_result_hash,
                "restoration_result_hash",
            ),
        )
        restoration = ArtifactOrchestrationSelectedStateRestorationResult.from_json(
            _payload_text(
                self.restoration_result_json,
                "restoration_result_json",
            )
        )
        restoration.verify_hash()
        if restoration.result_hash != self.restoration_result_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "restoration_result_hash does not match embedded result"
            )
        if restoration.status is not (
            ArtifactOrchestrationSelectedStateRestorationStatus.RESTORED
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "restoration result must have restored status"
            )
        object.__setattr__(
            self,
            "restoration_result_json",
            restoration.to_json(),
        )

        object.__setattr__(
            self,
            "approval_hash",
            _hash(self.approval_hash, "approval_hash"),
        )
        approval = ArtifactOrchestrationHumanResumeApproval.from_json(
            _payload_text(self.approval_json, "approval_json")
        )
        approval.verify_hash()
        if approval.approval_hash != self.approval_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "approval_hash does not match embedded approval"
            )
        if approval.restoration_result_hash != self.restoration_result_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError(
                "approval does not bind the restored selected state"
            )
        object.__setattr__(self, "approval_json", approval.to_json())

        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < approval.approved_at:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError(
                "requested_at cannot precede approved_at"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(
            self,
            "rationale",
            _text(self.rationale, "rationale"),
        )
        requested_steps = _step_ids(
            self.requested_step_ids,
            "requested_step_ids",
        )
        if not set(requested_steps).issubset(approval.approved_step_ids):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError(
                "requested steps exceed explicit human approval scope"
            )
        object.__setattr__(self, "requested_step_ids", requested_steps)

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "unsupported resume authorization format version"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_restoration_result(
        cls,
        restoration_result: ArtifactOrchestrationSelectedStateRestorationResult,
        approval: ArtifactOrchestrationHumanResumeApproval,
        policy: ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        rationale: str,
        requested_step_ids: tuple[str, ...] | list[str],
        authorization_id: str | None = None,
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationRequest":
        if not isinstance(
            restoration_result,
            ArtifactOrchestrationSelectedStateRestorationResult,
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "restoration_result must be an "
                "ArtifactOrchestrationSelectedStateRestorationResult"
            )
        if not isinstance(approval, ArtifactOrchestrationHumanResumeApproval):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "approval must be an ArtifactOrchestrationHumanResumeApproval"
            )
        if not isinstance(
            policy,
            ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy"
            )
        restoration_result.verify_hash()
        approval.verify_hash()
        result_hash = restoration_result.result_hash
        approval_hash = approval.approval_hash
        assert result_hash is not None
        assert approval_hash is not None
        normalized_requested_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_requested_at = _utc_timestamp(
            requested_at,
            "requested_at",
        )
        normalized_rationale = _text(rationale, "rationale")
        normalized_steps = _step_ids(
            requested_step_ids,
            "requested_step_ids",
        )
        identity_hash = _sha256_document(
            {
                "record_type": (
                    "artifact_orchestration_selected_state_resume_authorization_identity"
                ),
                "policy_hash": policy.policy_hash,
                "restoration_result_hash": result_hash,
                "approval_hash": approval_hash,
                "requested_by": normalized_requested_by,
                "requested_at": normalized_requested_at,
                "rationale": normalized_rationale,
                "requested_step_ids": list(normalized_steps),
            }
        )
        return cls(
            authorization_id=(
                authorization_id
                if authorization_id is not None
                else f"resume-authorization:{identity_hash}"
            ),
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            policy_json=policy.to_json(),
            restoration_result_hash=result_hash,
            restoration_result_json=restoration_result.to_json(),
            approval_hash=approval_hash,
            approval_json=approval.to_json(),
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            rationale=normalized_rationale,
            requested_step_ids=normalized_steps,
        )

    @property
    def policy(self) -> ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy:
        return ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy.from_json(
            self.policy_json
        )

    @property
    def restoration_result(
        self,
    ) -> ArtifactOrchestrationSelectedStateRestorationResult:
        return ArtifactOrchestrationSelectedStateRestorationResult.from_json(
            self.restoration_result_json
        )

    @property
    def approval(self) -> ArtifactOrchestrationHumanResumeApproval:
        return ArtifactOrchestrationHumanResumeApproval.from_json(
            self.approval_json
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_authorization_request"
            ),
            "version": self.version,
            "authorization_id": self.authorization_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "policy_json": self.policy_json,
            "restoration_result_hash": self.restoration_result_hash,
            "restoration_result_json": self.restoration_result_json,
            "approval_hash": self.approval_hash,
            "approval_json": self.approval_json,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
            "requested_step_ids": list(self.requested_step_ids),
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationRequest":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_authorization_request"
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_authorization_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            authorization_id=data["authorization_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            policy_json=data["policy_json"],
            restoration_result_hash=data["restoration_result_hash"],
            restoration_result_json=data["restoration_result_json"],
            approval_hash=data["approval_hash"],
            approval_json=data["approval_json"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            rationale=data["rationale"],
            requested_step_ids=tuple(data["requested_step_ids"]),
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeAuthorizationResult:
    authorization_id: str
    status: ArtifactOrchestrationSelectedStateResumeAuthorizationStatus
    authorization_request_json: str
    resume_request_json: str
    assessment_json: str
    decision_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorization_id",
            _identifier(self.authorization_id, "authorization_id"),
        )
        try:
            status = ArtifactOrchestrationSelectedStateResumeAuthorizationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        request = ArtifactOrchestrationSelectedStateResumeAuthorizationRequest.from_json(
            _payload_text(
                self.authorization_request_json,
                "authorization_request_json",
            )
        )
        request.verify_hash()
        if request.authorization_id != self.authorization_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "authorization_id does not match embedded request"
            )
        object.__setattr__(
            self,
            "authorization_request_json",
            request.to_json(),
        )

        try:
            resume_request = ResumeRequest.from_json(
                _payload_text(self.resume_request_json, "resume_request_json")
            )
        except ExecutionResumeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "embedded resume request is invalid"
            ) from exc
        object.__setattr__(
            self,
            "resume_request_json",
            resume_request.to_json(),
        )

        assessment = _assessment_from_json(
            _payload_text(self.assessment_json, "assessment_json")
        )
        object.__setattr__(self, "assessment_json", assessment.to_json())

        try:
            decision = ResumeDecision.from_json(
                _payload_text(self.decision_json, "decision_json")
            )
            decision.verify_hash()
        except ExecutionResumeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "embedded resume decision is invalid"
            ) from exc
        object.__setattr__(self, "decision_json", decision.to_json())

        restored = request.restoration_result.restored_state
        restored.verify_hash()
        approval = request.approval
        policy = request.policy
        checkpoint = restored.checkpoint
        checkpoint.verify_hash()

        if resume_request.request_id != f"resume-request:{request.request_hash}":
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "resume request identifier does not bind authorization request"
            )
        bindings = {
            "checkpoint_id": (
                resume_request.checkpoint_id,
                restored.checkpoint_id,
            ),
            "checkpoint_hash": (
                resume_request.checkpoint_hash,
                restored.checkpoint_hash,
            ),
            "plan_id": (resume_request.plan_id, restored.plan_id),
            "approval_reference": (
                resume_request.approval_reference,
                approval.approval_reference,
            ),
            "requested_by": (
                resume_request.requested_by,
                request.requested_by,
            ),
            "created_at": (
                resume_request.created_at,
                request.requested_at,
            ),
            "rationale": (resume_request.rationale, request.rationale),
            "requested_step_ids": (
                resume_request.requested_step_ids,
                request.requested_step_ids,
            ),
        }
        for name, (actual, expected) in bindings.items():
            if actual != expected:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    f"resume request {name} does not match authorization request"
                )

        if assessment.checkpoint_id != restored.checkpoint_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "assessment checkpoint does not match restored state"
            )
        if (
            policy.require_recomputed_assessment_match
            and assessment.to_json() != restored.assessment_json
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "assessment does not match restored-state assessment"
            )
        if decision.request_id != resume_request.request_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision request_id does not match resume request"
            )
        if decision.request_hash != resume_request.request_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision request_hash does not match resume request"
            )
        if decision.policy_id != policy.resume_policy.policy_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision policy_id does not match embedded resume policy"
            )
        if decision.policy_hash != policy.resume_policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision policy_hash does not match embedded resume policy"
            )
        if decision.checkpoint_id != restored.checkpoint_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision checkpoint_id does not match restored state"
            )
        if decision.checkpoint_hash != restored.checkpoint_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision checkpoint_hash does not match restored state"
            )
        if not set(decision.selected_step_ids).issubset(
            approval.approved_step_ids
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision exceeds explicit human approval scope"
            )
        if not set(decision.selected_step_ids).issubset(
            request.requested_step_ids
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "decision exceeds requested resume scope"
            )

        expected_status = (
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED
            if decision.status is ResumeDecisionStatus.APPROVED
            else ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.REJECTED
        )
        if status is not expected_status:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "authorization status does not match resume decision"
            )
        if status is (
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED
        ):
            if decision.command is None:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    "approved authorization requires a resume command"
                )
            if decision.command.approval_reference != approval.approval_reference:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    "resume command approval reference does not match evidence"
                )
        elif decision.command is not None:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "rejected authorization cannot contain a resume command"
            )

        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at != decision.issued_at:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "completed_at does not match resume decision"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_AUTHORIZATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "unsupported resume authorization format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def authorization_request(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeAuthorizationRequest:
        return ArtifactOrchestrationSelectedStateResumeAuthorizationRequest.from_json(
            self.authorization_request_json
        )

    @property
    def resume_request(self) -> ResumeRequest:
        return ResumeRequest.from_json(self.resume_request_json)

    @property
    def assessment(self) -> ResumeAssessment:
        return _assessment_from_json(self.assessment_json)

    @property
    def decision(self) -> ResumeDecision:
        return ResumeDecision.from_json(self.decision_json)

    @property
    def command(self) -> ResumeCommand | None:
        return self.decision.command

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_authorization_result"
            ),
            "version": self.version,
            "authorization_id": self.authorization_id,
            "status": self.status.value,
            "authorization_request_json": self.authorization_request_json,
            "resume_request_json": self.resume_request_json,
            "assessment_json": self.assessment_json,
            "decision_json": self.decision_json,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationResult":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_authorization_result"
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_authorization_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            authorization_id=data["authorization_id"],
            status=data["status"],
            authorization_request_json=data["authorization_request_json"],
            resume_request_json=data["resume_request_json"],
            assessment_json=data["assessment_json"],
            decision_json=data["decision_json"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeAuthorizationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "resume authorization result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeAuthorization:
    request: ArtifactOrchestrationSelectedStateResumeAuthorizationRequest
    policy: ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationSelectedStateResumeAuthorizationRequest,
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "request must be an "
                "ArtifactOrchestrationSelectedStateResumeAuthorizationRequest"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy"
            )
        self.request.verify_hash()
        if self.request.policy_id != self.policy.policy_id:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "request policy_id does not match policy"
            )
        if self.request.policy_hash != self.policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationError(
                "request policy_hash does not match policy"
            )
        if self.request.policy != self.policy:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "embedded request policy differs from supplied policy"
            )

    def authorize(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeAuthorizationResult:
        self.request.verify_hash()
        restoration = self.request.restoration_result
        restoration.verify_hash()
        approval = self.request.approval
        approval.verify_hash()
        restored = restoration.restored_state
        restored.verify_hash()

        plan = restored.plan
        journal = restored.journal
        checkpoint = restored.checkpoint
        checkpoint.verify_hash()

        before = {
            "restoration": restoration.to_json(),
            "plan": plan.to_json(),
            "journal": journal.to_jsonl(),
            "checkpoint": checkpoint.to_json(),
        }

        try:
            assessment = checkpoint.assess_resume(plan, journal)
        except (ExecutionCheckpointError, ValueError, TypeError) as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationExecutionError(
                "resume assessment recomputation failed"
            ) from exc

        if (
            self.policy.require_recomputed_assessment_match
            and assessment.to_json() != restored.assessment_json
        ):
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "recomputed resume assessment differs from restored state"
            )

        if self.policy.require_explicit_step_scope:
            if not set(self.request.requested_step_ids).issubset(
                approval.approved_step_ids
            ):
                raise ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError(
                    "requested steps exceed explicit human approval scope"
                )

        checkpoint_hash = checkpoint.checkpoint_hash
        assert checkpoint_hash is not None
        authorization_request_hash = self.request.request_hash
        assert authorization_request_hash is not None
        resume_request = ResumeRequest(
            request_id=f"resume-request:{authorization_request_hash}",
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            plan_id=checkpoint.plan_id,
            requested_by=self.request.requested_by,
            approval_reference=approval.approval_reference,
            created_at=self.request.requested_at,
            rationale=self.request.rationale,
            requested_step_ids=self.request.requested_step_ids,
        )

        try:
            decision = decide_resume(
                resume_request,
                checkpoint,
                assessment,
                self.policy.resume_policy,
                issued_at=self.request.requested_at,
            )
            decision.verify_hash()
        except ExecutionResumeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationExecutionError(
                "declarative resume authorization failed"
            ) from exc

        after = {
            "restoration": restoration.to_json(),
            "plan": plan.to_json(),
            "journal": journal.to_jsonl(),
            "checkpoint": checkpoint.to_json(),
        }
        if after != before:
            raise ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError(
                "resume authorization mutated source orchestration state"
            )

        status = (
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED
            if decision.status is ResumeDecisionStatus.APPROVED
            else ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.REJECTED
        )
        return ArtifactOrchestrationSelectedStateResumeAuthorizationResult(
            authorization_id=self.request.authorization_id,
            status=status,
            authorization_request_json=self.request.to_json(),
            resume_request_json=resume_request.to_json(),
            assessment_json=assessment.to_json(),
            decision_json=decision.to_json(),
            completed_at=decision.issued_at,
            reason=(
                f"{status.value.upper()}: "
                + "; ".join(decision.reasons)
            ),
        )
