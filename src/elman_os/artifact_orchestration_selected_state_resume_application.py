"""Controlled in-memory application of an authorized selected-state resume.

This boundary consumes a cryptographically verified, approved
``ArtifactOrchestrationSelectedStateResumeAuthorizationResult`` and delegates
its embedded ``ResumeCommand`` to the existing ``ResumeApplication`` boundary
against the exact restored plan, journal, and checkpoint that were authorized.

The component returns a fully verifiable updated plan and journal representation
but never persists them, executes a step, dispatches an agent, invokes an AI
provider, writes to a project workspace, or performs network access.
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
from .artifact_orchestration_selected_state_resume_authorization import (
    ArtifactOrchestrationSelectedStateResumeAuthorizationError,
    ArtifactOrchestrationSelectedStateResumeAuthorizationResult,
    ArtifactOrchestrationSelectedStateResumeAuthorizationStatus,
)
from .resume_application import (
    ResumeApplication,
    ResumeApplicationError,
    ResumeApplicationResult,
    ResumeApplicationStatus,
)


ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION: Final[
    int
] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactOrchestrationSelectedStateResumeApplicationError(RuntimeError):
    """An authorized selected-state resume cannot be applied safely."""


class ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
    ArtifactOrchestrationSelectedStateResumeApplicationError
):
    """The supplied authorization does not permit resume application."""


class ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
    ArtifactOrchestrationSelectedStateResumeApplicationError
):
    """A serialized boundary object or embedded result fails verification."""


class ArtifactOrchestrationSelectedStateResumeApplicationExecutionError(
    ArtifactOrchestrationSelectedStateResumeApplicationError
):
    """The delegated in-memory resume application failed."""


class ArtifactOrchestrationSelectedStateResumeApplicationStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already-applied"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
            f"{name} must be a non-empty string"
        )
    return value.strip()


def _payload_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
            f"{name} must be a non-empty JSON string"
        )
    return value


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
            f"{name} must be a boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                f"{name} datetime must already be in UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactOrchestrationSelectedStateResumeApplicationError(
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
class ArtifactOrchestrationSelectedStateResumeApplicationPolicy:
    policy_id: str
    require_approved_authorization: bool = True
    require_source_immutability: bool = True
    allow_already_applied: bool = True
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for name in (
            "require_approved_authorization",
            "require_source_immutability",
            "allow_already_applied",
        ):
            object.__setattr__(
                self,
                name,
                _boolean(getattr(self, name), name),
            )
        if not self.require_approved_authorization:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "resume application policy must require approved authorization"
            )
        if not self.require_source_immutability:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "resume application policy must require source immutability"
            )
        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "unsupported selected-state resume application format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_application_policy"
            ),
            "version": self.version,
            "policy_id": self.policy_id,
            "require_approved_authorization": self.require_approved_authorization,
            "require_source_immutability": self.require_source_immutability,
            "allow_already_applied": self.allow_already_applied,
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
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationPolicy":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_application_policy"
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_application_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            require_approved_authorization=data[
                "require_approved_authorization"
            ],
            require_source_immutability=data["require_source_immutability"],
            allow_already_applied=data["allow_already_applied"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationPolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeApplicationRequest:
    application_request_id: str
    policy_json: str
    policy_hash: str
    authorization_result_json: str
    authorization_result_hash: str
    requested_by: str
    requested_at: str
    rationale: str
    request_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_request_id",
            _identifier(self.application_request_id, "application_request_id"),
        )
        policy = ArtifactOrchestrationSelectedStateResumeApplicationPolicy.from_json(
            _payload_text(self.policy_json, "policy_json")
        )
        supplied_policy_hash = _hash(self.policy_hash, "policy_hash")
        if supplied_policy_hash != policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "policy_hash does not match embedded policy"
            )
        object.__setattr__(self, "policy_json", policy.to_json())
        object.__setattr__(self, "policy_hash", supplied_policy_hash)

        try:
            authorization = (
                ArtifactOrchestrationSelectedStateResumeAuthorizationResult.from_json(
                    _payload_text(
                        self.authorization_result_json,
                        "authorization_result_json",
                    )
                )
            )
            authorization.verify_hash()
        except ArtifactOrchestrationSelectedStateResumeAuthorizationError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "embedded resume authorization result is invalid"
            ) from exc
        supplied_authorization_hash = _hash(
            self.authorization_result_hash,
            "authorization_result_hash",
        )
        if supplied_authorization_hash != authorization.result_hash:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "authorization_result_hash does not match embedded authorization"
            )
        object.__setattr__(
            self,
            "authorization_result_json",
            authorization.to_json(),
        )
        object.__setattr__(
            self,
            "authorization_result_hash",
            supplied_authorization_hash,
        )
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        requested_at = _utc_timestamp(self.requested_at, "requested_at")
        if requested_at < authorization.completed_at:
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "resume application request cannot precede authorization"
            )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "unsupported selected-state resume application format version"
            )

        expected_id = f"resume-application-request:{self.compute_identity_hash()}"
        if self.application_request_id != expected_id:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "application_request_id does not match request identity"
            )

        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @property
    def policy(self) -> ArtifactOrchestrationSelectedStateResumeApplicationPolicy:
        return ArtifactOrchestrationSelectedStateResumeApplicationPolicy.from_json(
            self.policy_json
        )

    @property
    def authorization_result(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeAuthorizationResult:
        return ArtifactOrchestrationSelectedStateResumeAuthorizationResult.from_json(
            self.authorization_result_json
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "authorization_result_hash": self.authorization_result_hash,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
        }

    def compute_identity_hash(self) -> str:
        return _sha256_document(self.identity_material())

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_application_request"
            ),
            "version": self.version,
            "application_request_id": self.application_request_id,
            "policy_json": self.policy_json,
            "policy_hash": self.policy_hash,
            "authorization_result_json": self.authorization_result_json,
            "authorization_result_hash": self.authorization_result_hash,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "request hash does not match request content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["request_hash"] = self.request_hash
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_authorization_result(
        cls,
        *,
        authorization_result: ArtifactOrchestrationSelectedStateResumeAuthorizationResult,
        policy: ArtifactOrchestrationSelectedStateResumeApplicationPolicy,
        requested_by: str,
        requested_at: str | datetime,
        rationale: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationRequest":
        if not isinstance(
            authorization_result,
            ArtifactOrchestrationSelectedStateResumeAuthorizationResult,
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "authorization_result must be an "
                "ArtifactOrchestrationSelectedStateResumeAuthorizationResult"
            )
        if not isinstance(
            policy,
            ArtifactOrchestrationSelectedStateResumeApplicationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumeApplicationPolicy"
            )
        authorization_result.verify_hash()
        normalized_requested_by = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        normalized_requested_at = _utc_timestamp(requested_at, "requested_at")
        normalized_rationale = _text(rationale, "rationale")
        identity_hash = _sha256_document(
            {
                "policy_hash": policy.policy_hash,
                "authorization_result_hash": authorization_result.result_hash,
                "requested_by": normalized_requested_by,
                "requested_at": normalized_requested_at,
                "rationale": normalized_rationale,
            }
        )
        return cls(
            application_request_id=f"resume-application-request:{identity_hash}",
            policy_json=policy.to_json(),
            policy_hash=policy.policy_hash,
            authorization_result_json=authorization_result.to_json(),
            authorization_result_hash=authorization_result.result_hash or "",
            requested_by=normalized_requested_by,
            requested_at=normalized_requested_at,
            rationale=normalized_rationale,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationRequest":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_application_request"
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_application_request"
            )
        if "request_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            application_request_id=data["application_request_id"],
            policy_json=data["policy_json"],
            policy_hash=data["policy_hash"],
            authorization_result_json=data["authorization_result_json"],
            authorization_result_hash=data["authorization_result_hash"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            rationale=data["rationale"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeApplicationResult:
    application_request_id: str
    status: ArtifactOrchestrationSelectedStateResumeApplicationStatus
    application_request_json: str
    resume_application_result_json: str
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = (
        ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_request_id",
            _identifier(self.application_request_id, "application_request_id"),
        )
        try:
            status = ArtifactOrchestrationSelectedStateResumeApplicationStatus(
                self.status
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        request = ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_json(
            _payload_text(self.application_request_json, "application_request_json")
        )
        request.verify_hash()
        if request.application_request_id != self.application_request_id:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "application_request_id does not match embedded request"
            )
        object.__setattr__(self, "application_request_json", request.to_json())

        try:
            application = ResumeApplicationResult.from_json(
                _payload_text(
                    self.resume_application_result_json,
                    "resume_application_result_json",
                )
            )
            application.verify_hash()
        except ResumeApplicationError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "embedded resume application result is invalid"
            ) from exc
        object.__setattr__(
            self,
            "resume_application_result_json",
            application.to_json(),
        )

        authorization = request.authorization_result
        authorization.verify_hash()
        if authorization.status is not (
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "embedded authorization is not approved"
            )
        command = authorization.command
        if command is None:
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "approved authorization is missing resume command"
            )
        command.verify_hash()
        restored = authorization.authorization_request.restoration_result.restored_state
        restored.verify_hash()

        bindings = {
            "command_id": (application.command_id, command.command_id),
            "command_hash": (application.command_hash, command.command_hash),
            "checkpoint_id": (
                application.checkpoint_id,
                restored.checkpoint_id,
            ),
            "checkpoint_hash": (
                application.checkpoint_hash,
                restored.checkpoint_hash,
            ),
            "plan_id": (application.plan_id, restored.plan_id),
            "selected_step_ids": (
                application.selected_step_ids,
                command.selected_step_ids,
            ),
        }
        for name, (actual, expected) in bindings.items():
            if actual != expected:
                raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                    f"resume application {name} does not match authorization"
                )

        expected_status = (
            ArtifactOrchestrationSelectedStateResumeApplicationStatus.APPLIED
            if application.status is ResumeApplicationStatus.APPLIED
            else ArtifactOrchestrationSelectedStateResumeApplicationStatus.ALREADY_APPLIED
        )
        if status is not expected_status:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "boundary status does not match resume application result"
            )
        if (
            status
            is ArtifactOrchestrationSelectedStateResumeApplicationStatus.ALREADY_APPLIED
            and not request.policy.allow_already_applied
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "policy forbids already-applied resume results"
            )

        completed_at = _utc_timestamp(self.completed_at, "completed_at")
        if completed_at != application.applied_at:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "completed_at does not match resume application result"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

        if self.version != (
            ARTIFACT_ORCHESTRATION_SELECTED_STATE_RESUME_APPLICATION_FORMAT_VERSION
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "unsupported selected-state resume application format version"
            )

        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    @property
    def application_request(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeApplicationRequest:
        return ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_json(
            self.application_request_json
        )

    @property
    def resume_application_result(self) -> ResumeApplicationResult:
        return ResumeApplicationResult.from_json(
            self.resume_application_result_json
        )

    @property
    def updated_plan(self):
        return self.resume_application_result.updated_plan

    @property
    def updated_journal(self):
        return self.resume_application_result.to_journal()

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": (
                "artifact_orchestration_selected_state_resume_application_result"
            ),
            "version": self.version,
            "application_request_id": self.application_request_id,
            "status": self.status.value,
            "application_request_json": self.application_request_json,
            "resume_application_result_json": (
                self.resume_application_result_json
            ),
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
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
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationResult":
        if data.get("record_type") != (
            "artifact_orchestration_selected_state_resume_application_result"
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "record_type must be "
                "artifact_orchestration_selected_state_resume_application_result"
            )
        if "result_hash" not in data:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            application_request_id=data["application_request_id"],
            status=data["status"],
            application_request_json=data["application_request_json"],
            resume_application_result_json=data[
                "resume_application_result_json"
            ],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactOrchestrationSelectedStateResumeApplicationResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "selected-state resume application result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactOrchestrationSelectedStateResumeApplication:
    request: ArtifactOrchestrationSelectedStateResumeApplicationRequest
    policy: ArtifactOrchestrationSelectedStateResumeApplicationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.request,
            ArtifactOrchestrationSelectedStateResumeApplicationRequest,
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "request must be an "
                "ArtifactOrchestrationSelectedStateResumeApplicationRequest"
            )
        if not isinstance(
            self.policy,
            ArtifactOrchestrationSelectedStateResumeApplicationPolicy,
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "policy must be an "
                "ArtifactOrchestrationSelectedStateResumeApplicationPolicy"
            )
        self.request.verify_hash()
        if self.request.policy_hash != self.policy.policy_hash:
            raise ArtifactOrchestrationSelectedStateResumeApplicationError(
                "request policy_hash does not match supplied policy"
            )
        if self.request.policy != self.policy:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "embedded request policy differs from supplied policy"
            )

    def apply(
        self,
    ) -> ArtifactOrchestrationSelectedStateResumeApplicationResult:
        self.request.verify_hash()
        authorization = self.request.authorization_result
        authorization.verify_hash()

        if (
            self.policy.require_approved_authorization
            and authorization.status
            is not ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "resume authorization is not approved"
            )
        command = authorization.command
        if command is None:
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "approved authorization does not contain a resume command"
            )
        command.verify_hash()

        restored = authorization.authorization_request.restoration_result.restored_state
        restored.verify_hash()
        plan = restored.plan
        journal = restored.journal
        checkpoint = restored.checkpoint
        checkpoint.verify_hash()

        before = {
            "authorization": authorization.to_json(),
            "plan": plan.to_json(),
            "journal": journal.to_jsonl(),
            "checkpoint": checkpoint.to_json(),
        }

        try:
            application = ResumeApplication(command, checkpoint).apply(
                plan,
                journal,
            )
            application.verify_hash()
        except ResumeApplicationError as exc:
            raise ArtifactOrchestrationSelectedStateResumeApplicationExecutionError(
                "authorized resume command application failed"
            ) from exc

        after = {
            "authorization": authorization.to_json(),
            "plan": plan.to_json(),
            "journal": journal.to_jsonl(),
            "checkpoint": checkpoint.to_json(),
        }
        if self.policy.require_source_immutability and after != before:
            raise ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError(
                "resume application mutated source restored state"
            )

        if (
            application.status is ResumeApplicationStatus.ALREADY_APPLIED
            and not self.policy.allow_already_applied
        ):
            raise ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError(
                "policy forbids already-applied resume results"
            )

        status = (
            ArtifactOrchestrationSelectedStateResumeApplicationStatus.APPLIED
            if application.status is ResumeApplicationStatus.APPLIED
            else ArtifactOrchestrationSelectedStateResumeApplicationStatus.ALREADY_APPLIED
        )
        return ArtifactOrchestrationSelectedStateResumeApplicationResult(
            application_request_id=self.request.application_request_id,
            status=status,
            application_request_json=self.request.to_json(),
            resume_application_result_json=application.to_json(),
            completed_at=application.applied_at,
            reason=(
                f"{status.value.upper()}: authorized resume command was "
                "applied in memory without persistence"
            ),
        )
