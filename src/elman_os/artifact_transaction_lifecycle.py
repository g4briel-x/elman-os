"""Coordinate the complete ELMAN-OS artifact transaction lifecycle.

The lifecycle coordinator composes the already validated low-level stages:
application, reconciliation, and recovery. It does not duplicate filesystem
mutation logic. Every route is selected from a fresh reconciliation result,
and every lower-level result is cryptographically linked into a deterministic
lifecycle journal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .artifact_application_plan import ArtifactApplicationPlan
from .artifact_payload_verification import ArtifactPayloadVerificationResult
from .artifact_transaction_application import (
    ArtifactTransactionApplication,
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
    ArtifactTransactionResult,
    ArtifactTransactionStatus,
)
from .artifact_transaction_reconciliation import (
    ArtifactTransactionReconciliation,
    ArtifactTransactionReconciliationPolicy,
    ArtifactTransactionReconciliationRequest,
    ArtifactTransactionReconciliationResult,
    ArtifactTransactionReconciliationStatus,
)
from .artifact_transaction_recovery_execution import (
    ArtifactTransactionRecoveryExecution,
    ArtifactTransactionRecoveryPolicy,
    ArtifactTransactionRecoveryRequest,
    ArtifactTransactionRecoveryResult,
    ArtifactTransactionRecoveryStatus,
)
from .artifact_workspace_preflight import ArtifactWorkspacePreflightResult


ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactTransactionLifecycleError(RuntimeError):
    """A lifecycle contract or coordination boundary is invalid."""


class ArtifactTransactionLifecycleIntegrityError(
    ArtifactTransactionLifecycleError
):
    """A lifecycle request, record, or result fails integrity."""


class ArtifactTransactionLifecycleState(StrEnum):
    CLEAN = "clean"
    APPLY_REQUIRED = "apply-required"
    COMMITTED = "committed"
    RECOVERY_REQUIRED = "recovery-required"
    RECOVERED = "recovered"
    CONFLICTED = "conflicted"
    FAILED = "failed"


class ArtifactTransactionLifecycleRoute(StrEnum):
    INSPECT_ONLY = "inspect-only"
    APPLY = "apply"
    VERIFY_COMMITTED = "verify-committed"
    RECOVER = "recover"
    RECOVER_THEN_APPLY = "recover-then-apply"
    REFUSE = "refuse"


class ArtifactTransactionLifecyclePhase(StrEnum):
    RECONCILE = "reconcile"
    APPLICATION = "application"
    RECOVERY = "recovery"
    POST_RECOVERY_RECONCILE = "post-recovery-reconcile"
    COMMITTED_VERIFICATION = "committed-verification"


class ArtifactTransactionLifecycleRecordStatus(StrEnum):
    COMPLETED = "completed"
    DEFERRED = "deferred"
    REFUSED = "refused"
    FAILED = "failed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactTransactionLifecycleError(
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
        raise ArtifactTransactionLifecycleError(
            f"{name} has an invalid format"
        )
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ArtifactTransactionLifecycleError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArtifactTransactionLifecycleError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactTransactionLifecycleError(
            f"{name} must be a non-negative integer"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactTransactionLifecycleError(
            f"{name} must be boolean"
        )
    return value


def _utc_timestamp(value: object, name: str) -> str:
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ArtifactTransactionLifecycleError(
                f"{name} datetime must be timezone-aware UTC"
            )
        if offset.total_seconds() != 0:
            raise ArtifactTransactionLifecycleError(
                f"{name} datetime must already be UTC"
            )
        parsed = value.astimezone(UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.endswith("Z") or "T" not in raw:
            raise ArtifactTransactionLifecycleError(
                f"{name} must be ISO-8601 UTC ending in Z"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ArtifactTransactionLifecycleError(
                f"{name} is not valid ISO-8601 UTC"
            ) from exc
        if (
            parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise ArtifactTransactionLifecycleError(
                f"{name} must be UTC"
            )
    else:
        raise ArtifactTransactionLifecycleError(
            f"{name} must be a UTC datetime or ISO-8601 string"
        )
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactTransactionLifecyclePolicy:
    policy_id: str
    auto_apply_when_clean: bool = True
    auto_recover_when_recoverable: bool = True
    apply_after_recovery: bool = False
    verify_committed_state: bool = True
    max_transitions: int = 6
    version: int = ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        for field_name in (
            "auto_apply_when_clean",
            "auto_recover_when_recoverable",
            "apply_after_recovery",
            "verify_committed_state",
        ):
            object.__setattr__(
                self,
                field_name,
                _boolean(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "max_transitions",
            _positive_int(self.max_transitions, "max_transitions"),
        )
        if self.max_transitions < 2:
            raise ArtifactTransactionLifecycleError(
                "max_transitions must allow reconciliation and routing"
            )
        if self.version != ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION:
            raise ArtifactTransactionLifecycleError(
                "unsupported lifecycle format version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_lifecycle_policy",
            "version": self.version,
            "policy_id": self.policy_id,
            "auto_apply_when_clean": self.auto_apply_when_clean,
            "auto_recover_when_recoverable": (
                self.auto_recover_when_recoverable
            ),
            "apply_after_recovery": self.apply_after_recovery,
            "verify_committed_state": self.verify_committed_state,
            "max_transitions": self.max_transitions,
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
    ) -> "ArtifactTransactionLifecyclePolicy":
        if (
            data.get("record_type")
            != "artifact_transaction_lifecycle_policy"
        ):
            raise ArtifactTransactionLifecycleError(
                "record_type must be artifact_transaction_lifecycle_policy"
            )
        return cls(
            policy_id=data["policy_id"],
            auto_apply_when_clean=data["auto_apply_when_clean"],
            auto_recover_when_recoverable=data[
                "auto_recover_when_recoverable"
            ],
            apply_after_recovery=data["apply_after_recovery"],
            verify_committed_state=data["verify_committed_state"],
            max_transitions=data["max_transitions"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionLifecyclePolicy":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionLifecycleError(
                "lifecycle policy JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionLifecycleError(
                "lifecycle policy JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionLifecycleRequest:
    lifecycle_id: str
    policy_id: str
    policy_hash: str
    transaction_id: str
    transaction_request_hash: str
    transaction_policy_id: str
    transaction_policy_hash: str
    reconciliation_policy_id: str
    reconciliation_policy_hash: str
    recovery_policy_id: str
    recovery_policy_hash: str
    application_id: str
    application_plan_hash: str
    verification_id: str
    verification_result_hash: str
    preflight_id: str
    preflight_result_hash: str
    snapshot_hash: str
    plan_id: str
    step_id: str
    agent_id: str
    workspace_root: str
    requested_by: str
    requested_at: str
    request_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "lifecycle_id",
            "policy_id",
            "transaction_id",
            "transaction_policy_id",
            "reconciliation_policy_id",
            "recovery_policy_id",
            "application_id",
            "verification_id",
            "preflight_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "policy_hash",
            "transaction_request_hash",
            "transaction_policy_hash",
            "reconciliation_policy_hash",
            "recovery_policy_hash",
            "application_plan_hash",
            "verification_result_hash",
            "preflight_result_hash",
            "snapshot_hash",
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
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionLifecycleError(
                "workspace_root must be absolute"
            )
        object.__setattr__(self, "workspace_root", Path(root).as_posix())
        object.__setattr__(
            self,
            "requested_by",
            _identifier(self.requested_by, "requested_by", _AGENT_ID),
        )
        object.__setattr__(
            self,
            "requested_at",
            _utc_timestamp(self.requested_at, "requested_at"),
        )
        if self.version != ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION:
            raise ArtifactTransactionLifecycleError(
                "unsupported lifecycle format version"
            )
        computed = self.compute_hash()
        if self.request_hash is None:
            object.__setattr__(self, "request_hash", computed)
        else:
            supplied = _hash(self.request_hash, "request_hash")
            if supplied != computed:
                raise ArtifactTransactionLifecycleIntegrityError(
                    "request hash does not match request content"
                )
            object.__setattr__(self, "request_hash", supplied)

    @classmethod
    def from_sources(
        cls,
        transaction_request: ArtifactTransactionRequest,
        transaction_policy: ArtifactTransactionPolicy,
        application_plan: ArtifactApplicationPlan,
        verification_result: ArtifactPayloadVerificationResult,
        preflight_result: ArtifactWorkspacePreflightResult,
        reconciliation_policy: ArtifactTransactionReconciliationPolicy,
        recovery_policy: ArtifactTransactionRecoveryPolicy,
        policy: ArtifactTransactionLifecyclePolicy,
        *,
        requested_by: str,
        requested_at: str | datetime,
        lifecycle_id: str | None = None,
    ) -> "ArtifactTransactionLifecycleRequest":
        transaction_request.verify_hash()
        application_plan.verify_hash()
        verification_result.verify_hash()
        preflight_result.verify_hash()
        transaction_hash = transaction_request.request_hash
        plan_hash = application_plan.plan_hash
        verification_hash = verification_result.result_hash
        preflight_hash = preflight_result.result_hash
        assert transaction_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None
        requester = _identifier(
            requested_by,
            "requested_by",
            _AGENT_ID,
        )
        timestamp = _utc_timestamp(requested_at, "requested_at")
        identity_hash = _sha256_document(
            {
                "record_type": "artifact_transaction_lifecycle_identity",
                "policy_hash": policy.policy_hash,
                "transaction_request_hash": transaction_hash,
                "reconciliation_policy_hash": (
                    reconciliation_policy.policy_hash
                ),
                "recovery_policy_hash": recovery_policy.policy_hash,
                "workspace_root": transaction_request.workspace_root,
            }
        )
        effective_id = (
            lifecycle_id
            if lifecycle_id is not None
            else f"transaction-lifecycle:{identity_hash}"
        )
        return cls(
            lifecycle_id=effective_id,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            transaction_id=transaction_request.transaction_id,
            transaction_request_hash=transaction_hash,
            transaction_policy_id=transaction_policy.policy_id,
            transaction_policy_hash=transaction_policy.policy_hash,
            reconciliation_policy_id=reconciliation_policy.policy_id,
            reconciliation_policy_hash=reconciliation_policy.policy_hash,
            recovery_policy_id=recovery_policy.policy_id,
            recovery_policy_hash=recovery_policy.policy_hash,
            application_id=application_plan.application_id,
            application_plan_hash=plan_hash,
            verification_id=verification_result.verification_id,
            verification_result_hash=verification_hash,
            preflight_id=preflight_result.preflight_id,
            preflight_result_hash=preflight_hash,
            snapshot_hash=preflight_result.snapshot_hash,
            plan_id=application_plan.plan_id,
            step_id=application_plan.step_id,
            agent_id=application_plan.agent_id,
            workspace_root=transaction_request.workspace_root,
            requested_by=requester,
            requested_at=timestamp,
        )

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_lifecycle_request",
            "version": self.version,
            "lifecycle_id": self.lifecycle_id,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "transaction_id": self.transaction_id,
            "transaction_request_hash": self.transaction_request_hash,
            "transaction_policy_id": self.transaction_policy_id,
            "transaction_policy_hash": self.transaction_policy_hash,
            "reconciliation_policy_id": self.reconciliation_policy_id,
            "reconciliation_policy_hash": (
                self.reconciliation_policy_hash
            ),
            "recovery_policy_id": self.recovery_policy_id,
            "recovery_policy_hash": self.recovery_policy_hash,
            "application_id": self.application_id,
            "application_plan_hash": self.application_plan_hash,
            "verification_id": self.verification_id,
            "verification_result_hash": self.verification_result_hash,
            "preflight_id": self.preflight_id,
            "preflight_result_hash": self.preflight_result_hash,
            "snapshot_hash": self.snapshot_hash,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.request_hash != self.compute_hash():
            raise ArtifactTransactionLifecycleIntegrityError(
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
    ) -> "ArtifactTransactionLifecycleRequest":
        if (
            data.get("record_type")
            != "artifact_transaction_lifecycle_request"
        ):
            raise ArtifactTransactionLifecycleError(
                "record_type must be artifact_transaction_lifecycle_request"
            )
        if "request_hash" not in data:
            raise ArtifactTransactionLifecycleIntegrityError(
                "serialized request is missing request_hash"
            )
        return cls(
            lifecycle_id=data["lifecycle_id"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data["transaction_request_hash"],
            transaction_policy_id=data["transaction_policy_id"],
            transaction_policy_hash=data["transaction_policy_hash"],
            reconciliation_policy_id=data["reconciliation_policy_id"],
            reconciliation_policy_hash=data[
                "reconciliation_policy_hash"
            ],
            recovery_policy_id=data["recovery_policy_id"],
            recovery_policy_hash=data["recovery_policy_hash"],
            application_id=data["application_id"],
            application_plan_hash=data["application_plan_hash"],
            verification_id=data["verification_id"],
            verification_result_hash=data["verification_result_hash"],
            preflight_id=data["preflight_id"],
            preflight_result_hash=data["preflight_result_hash"],
            snapshot_hash=data["snapshot_hash"],
            plan_id=data["plan_id"],
            step_id=data["step_id"],
            agent_id=data["agent_id"],
            workspace_root=data["workspace_root"],
            requested_by=data["requested_by"],
            requested_at=data["requested_at"],
            request_hash=data["request_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionLifecycleRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionLifecycleError(
                "lifecycle request JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionLifecycleError(
                "lifecycle request JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionLifecycleRecord:
    index: int
    phase: ArtifactTransactionLifecyclePhase
    status: ArtifactTransactionLifecycleRecordStatus
    state_before: ArtifactTransactionLifecycleState
    state_after: ArtifactTransactionLifecycleState
    component_id: str
    component_result_hash: str | None
    reason: str
    record_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index",
            _non_negative_int(self.index, "index"),
        )
        for field_name, enum_type in (
            ("phase", ArtifactTransactionLifecyclePhase),
            ("status", ArtifactTransactionLifecycleRecordStatus),
            ("state_before", ArtifactTransactionLifecycleState),
            ("state_after", ArtifactTransactionLifecycleState),
        ):
            try:
                value = enum_type(getattr(self, field_name))
            except (TypeError, ValueError) as exc:
                raise ArtifactTransactionLifecycleError(
                    f"{field_name} is invalid"
                ) from exc
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self,
            "component_id",
            _identifier(self.component_id, "component_id"),
        )
        if self.component_result_hash is not None:
            object.__setattr__(
                self,
                "component_result_hash",
                _hash(
                    self.component_result_hash,
                    "component_result_hash",
                ),
            )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        computed = self.compute_hash()
        if self.record_hash is None:
            object.__setattr__(self, "record_hash", computed)
        else:
            supplied = _hash(self.record_hash, "record_hash")
            if supplied != computed:
                raise ArtifactTransactionLifecycleIntegrityError(
                    "record hash does not match record content"
                )
            object.__setattr__(self, "record_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "phase": self.phase.value,
            "status": self.status.value,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "component_id": self.component_id,
            "component_result_hash": self.component_result_hash,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.record_hash != self.compute_hash():
            raise ArtifactTransactionLifecycleIntegrityError(
                "record hash does not match record content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["record_hash"] = self.record_hash
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ArtifactTransactionLifecycleRecord":
        return cls(
            index=data["index"],
            phase=ArtifactTransactionLifecyclePhase(data["phase"]),
            status=ArtifactTransactionLifecycleRecordStatus(
                data["status"]
            ),
            state_before=ArtifactTransactionLifecycleState(
                data["state_before"]
            ),
            state_after=ArtifactTransactionLifecycleState(
                data["state_after"]
            ),
            component_id=data["component_id"],
            component_result_hash=data.get("component_result_hash"),
            reason=data["reason"],
            record_hash=data.get("record_hash"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactTransactionLifecycleResult:
    lifecycle_id: str
    final_state: ArtifactTransactionLifecycleState
    route: ArtifactTransactionLifecycleRoute
    request_hash: str
    policy_id: str
    policy_hash: str
    transaction_id: str
    transaction_request_hash: str
    application_plan_hash: str
    verification_result_hash: str
    preflight_result_hash: str
    workspace_root: str
    records: tuple[ArtifactTransactionLifecycleRecord, ...]
    initial_reconciliation_result_hash: str
    final_reconciliation_result_hash: str | None
    transaction_result_hash: str | None
    recovery_result_hash: str | None
    transition_count: int
    completed_at: str
    reason: str
    result_hash: str | None = None
    version: int = ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lifecycle_id",
            _identifier(self.lifecycle_id, "lifecycle_id"),
        )
        try:
            state = ArtifactTransactionLifecycleState(self.final_state)
            route = ArtifactTransactionLifecycleRoute(self.route)
        except (TypeError, ValueError) as exc:
            raise ArtifactTransactionLifecycleError(
                "lifecycle state or route is invalid"
            ) from exc
        object.__setattr__(self, "final_state", state)
        object.__setattr__(self, "route", route)
        for field_name in (
            "request_hash",
            "policy_hash",
            "transaction_request_hash",
            "application_plan_hash",
            "verification_result_hash",
            "preflight_result_hash",
            "initial_reconciliation_result_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        for field_name in (
            "final_reconciliation_result_hash",
            "transaction_result_hash",
            "recovery_result_hash",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _hash(value, field_name),
                )
        object.__setattr__(
            self,
            "policy_id",
            _identifier(self.policy_id, "policy_id"),
        )
        object.__setattr__(
            self,
            "transaction_id",
            _identifier(self.transaction_id, "transaction_id"),
        )
        root = _text(self.workspace_root, "workspace_root")
        if not Path(root).is_absolute():
            raise ArtifactTransactionLifecycleError(
                "workspace_root must be absolute"
            )
        object.__setattr__(self, "workspace_root", Path(root).as_posix())
        records = tuple(self.records)
        if not all(
            isinstance(item, ArtifactTransactionLifecycleRecord)
            for item in records
        ):
            raise ArtifactTransactionLifecycleError(
                "records must contain lifecycle records"
            )
        if tuple(item.index for item in records) != tuple(range(len(records))):
            raise ArtifactTransactionLifecycleError(
                "record indexes must be contiguous from zero"
            )
        for item in records:
            item.verify_hash()
        object.__setattr__(self, "records", records)
        object.__setattr__(
            self,
            "transition_count",
            _non_negative_int(self.transition_count, "transition_count"),
        )
        if self.transition_count != len(records):
            raise ArtifactTransactionLifecycleIntegrityError(
                "transition_count does not match records"
            )
        if records and records[-1].state_after is not state:
            raise ArtifactTransactionLifecycleIntegrityError(
                "final_state does not match the last lifecycle record"
            )
        if state is ArtifactTransactionLifecycleState.CONFLICTED:
            if route is not ArtifactTransactionLifecycleRoute.REFUSE:
                raise ArtifactTransactionLifecycleIntegrityError(
                    "conflicted lifecycle must use refuse route"
                )
        if state is ArtifactTransactionLifecycleState.COMMITTED:
            if self.transaction_result_hash is None:
                raise ArtifactTransactionLifecycleIntegrityError(
                    "committed lifecycle requires transaction_result_hash"
                )
        object.__setattr__(
            self,
            "completed_at",
            _utc_timestamp(self.completed_at, "completed_at"),
        )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.version != ARTIFACT_TRANSACTION_LIFECYCLE_FORMAT_VERSION:
            raise ArtifactTransactionLifecycleError(
                "unsupported lifecycle format version"
            )
        computed = self.compute_hash()
        if self.result_hash is None:
            object.__setattr__(self, "result_hash", computed)
        else:
            supplied = _hash(self.result_hash, "result_hash")
            if supplied != computed:
                raise ArtifactTransactionLifecycleIntegrityError(
                    "result hash does not match result content"
                )
            object.__setattr__(self, "result_hash", supplied)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "artifact_transaction_lifecycle_result",
            "version": self.version,
            "lifecycle_id": self.lifecycle_id,
            "final_state": self.final_state.value,
            "route": self.route.value,
            "request_hash": self.request_hash,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "transaction_id": self.transaction_id,
            "transaction_request_hash": self.transaction_request_hash,
            "application_plan_hash": self.application_plan_hash,
            "verification_result_hash": self.verification_result_hash,
            "preflight_result_hash": self.preflight_result_hash,
            "workspace_root": self.workspace_root,
            "records": [item.to_dict() for item in self.records],
            "initial_reconciliation_result_hash": (
                self.initial_reconciliation_result_hash
            ),
            "final_reconciliation_result_hash": (
                self.final_reconciliation_result_hash
            ),
            "transaction_result_hash": self.transaction_result_hash,
            "recovery_result_hash": self.recovery_result_hash,
            "transition_count": self.transition_count,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.result_hash != self.compute_hash():
            raise ArtifactTransactionLifecycleIntegrityError(
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
    ) -> "ArtifactTransactionLifecycleResult":
        if (
            data.get("record_type")
            != "artifact_transaction_lifecycle_result"
        ):
            raise ArtifactTransactionLifecycleError(
                "record_type must be artifact_transaction_lifecycle_result"
            )
        if "result_hash" not in data:
            raise ArtifactTransactionLifecycleIntegrityError(
                "serialized result is missing result_hash"
            )
        return cls(
            lifecycle_id=data["lifecycle_id"],
            final_state=ArtifactTransactionLifecycleState(
                data["final_state"]
            ),
            route=ArtifactTransactionLifecycleRoute(data["route"]),
            request_hash=data["request_hash"],
            policy_id=data["policy_id"],
            policy_hash=data["policy_hash"],
            transaction_id=data["transaction_id"],
            transaction_request_hash=data["transaction_request_hash"],
            application_plan_hash=data["application_plan_hash"],
            verification_result_hash=data["verification_result_hash"],
            preflight_result_hash=data["preflight_result_hash"],
            workspace_root=data["workspace_root"],
            records=tuple(
                ArtifactTransactionLifecycleRecord.from_dict(item)
                for item in data["records"]
            ),
            initial_reconciliation_result_hash=data[
                "initial_reconciliation_result_hash"
            ],
            final_reconciliation_result_hash=data.get(
                "final_reconciliation_result_hash"
            ),
            transaction_result_hash=data.get("transaction_result_hash"),
            recovery_result_hash=data.get("recovery_result_hash"),
            transition_count=data["transition_count"],
            completed_at=data["completed_at"],
            reason=data["reason"],
            result_hash=data["result_hash"],
            version=data.get("version", 0),
        )

    @classmethod
    def from_json(
        cls,
        payload: str,
    ) -> "ArtifactTransactionLifecycleResult":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactTransactionLifecycleError(
                "lifecycle result JSON is invalid"
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactTransactionLifecycleError(
                "lifecycle result JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ArtifactTransactionLifecycleCoordinator:
    request: ArtifactTransactionLifecycleRequest
    transaction_request: ArtifactTransactionRequest
    transaction_policy: ArtifactTransactionPolicy
    application_plan: ArtifactApplicationPlan
    verification_result: ArtifactPayloadVerificationResult
    preflight_result: ArtifactWorkspacePreflightResult
    reconciliation_policy: ArtifactTransactionReconciliationPolicy
    recovery_policy: ArtifactTransactionRecoveryPolicy
    policy: ArtifactTransactionLifecyclePolicy

    def __post_init__(self) -> None:
        self.request.verify_hash()
        self.transaction_request.verify_hash()
        self.application_plan.verify_hash()
        self.verification_result.verify_hash()
        self.preflight_result.verify_hash()
        transaction_hash = self.transaction_request.request_hash
        plan_hash = self.application_plan.plan_hash
        verification_hash = self.verification_result.result_hash
        preflight_hash = self.preflight_result.result_hash
        assert transaction_hash is not None
        assert plan_hash is not None
        assert verification_hash is not None
        assert preflight_hash is not None
        expected = {
            "policy_id": self.policy.policy_id,
            "policy_hash": self.policy.policy_hash,
            "transaction_id": self.transaction_request.transaction_id,
            "transaction_request_hash": transaction_hash,
            "transaction_policy_id": self.transaction_policy.policy_id,
            "transaction_policy_hash": self.transaction_policy.policy_hash,
            "reconciliation_policy_id": self.reconciliation_policy.policy_id,
            "reconciliation_policy_hash": (
                self.reconciliation_policy.policy_hash
            ),
            "recovery_policy_id": self.recovery_policy.policy_id,
            "recovery_policy_hash": self.recovery_policy.policy_hash,
            "application_id": self.application_plan.application_id,
            "application_plan_hash": plan_hash,
            "verification_id": self.verification_result.verification_id,
            "verification_result_hash": verification_hash,
            "preflight_id": self.preflight_result.preflight_id,
            "preflight_result_hash": preflight_hash,
            "snapshot_hash": self.preflight_result.snapshot_hash,
            "plan_id": self.application_plan.plan_id,
            "step_id": self.application_plan.step_id,
            "agent_id": self.application_plan.agent_id,
            "workspace_root": self.transaction_request.workspace_root,
        }
        for field_name, expected_value in expected.items():
            if getattr(self.request, field_name) != expected_value:
                raise ArtifactTransactionLifecycleError(
                    f"request {field_name} does not match lifecycle source"
                )

    def run(self) -> ArtifactTransactionLifecycleResult:
        records: list[ArtifactTransactionLifecycleRecord] = []
        initial = self._reconcile()
        initial_hash = initial.result_hash
        assert initial_hash is not None
        self._append_record(
            records,
            phase=ArtifactTransactionLifecyclePhase.RECONCILE,
            status=ArtifactTransactionLifecycleRecordStatus.COMPLETED,
            state_before=ArtifactTransactionLifecycleState.CLEAN,
            state_after=self._state_from_reconciliation(initial),
            component_id=initial.reconciliation_id,
            component_result_hash=initial_hash,
            reason=(
                "COMPLETED: transaction state was classified by a fresh "
                "read-only reconciliation"
            ),
        )

        if initial.status is ArtifactTransactionReconciliationStatus.CONFLICTED:
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.CONFLICTED,
                route=ArtifactTransactionLifecycleRoute.REFUSE,
                initial=initial,
                final_reconciliation=initial,
                transaction_result=None,
                recovery_result=None,
                reason=(
                    "REFUSED: reconciliation reported a conflicted "
                    "transaction state"
                ),
            )

        if initial.status is ArtifactTransactionReconciliationStatus.COMMITTED:
            return self._handle_committed(records, initial)

        if initial.status is ArtifactTransactionReconciliationStatus.CLEAN:
            if not self.policy.auto_apply_when_clean:
                self._append_record(
                    records,
                    phase=ArtifactTransactionLifecyclePhase.APPLICATION,
                    status=ArtifactTransactionLifecycleRecordStatus.DEFERRED,
                    state_before=ArtifactTransactionLifecycleState.CLEAN,
                    state_after=(
                        ArtifactTransactionLifecycleState.APPLY_REQUIRED
                    ),
                    component_id=self.transaction_request.transaction_id,
                    component_result_hash=None,
                    reason=(
                        "DEFERRED: lifecycle policy requires an external "
                        "application decision"
                    ),
                )
                return self._result(
                    records=records,
                    final_state=(
                        ArtifactTransactionLifecycleState.APPLY_REQUIRED
                    ),
                    route=ArtifactTransactionLifecycleRoute.INSPECT_ONLY,
                    initial=initial,
                    final_reconciliation=initial,
                    transaction_result=None,
                    recovery_result=None,
                    reason="DEFERRED: clean transaction awaits application",
                )
            return self._apply(records, initial, route=ArtifactTransactionLifecycleRoute.APPLY)

        if not self.policy.auto_recover_when_recoverable:
            self._append_record(
                records,
                phase=ArtifactTransactionLifecyclePhase.RECOVERY,
                status=ArtifactTransactionLifecycleRecordStatus.DEFERRED,
                state_before=ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
                state_after=ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
                component_id=initial.reconciliation_id,
                component_result_hash=initial_hash,
                reason=(
                    "DEFERRED: lifecycle policy requires an external "
                    "recovery decision"
                ),
            )
            return self._result(
                records=records,
                final_state=(
                    ArtifactTransactionLifecycleState.RECOVERY_REQUIRED
                ),
                route=ArtifactTransactionLifecycleRoute.INSPECT_ONLY,
                initial=initial,
                final_reconciliation=initial,
                transaction_result=None,
                recovery_result=None,
                reason="DEFERRED: recoverable transaction awaits recovery",
            )

        return self._recover(records, initial)

    def _reconcile(self) -> ArtifactTransactionReconciliationResult:
        request = ArtifactTransactionReconciliationRequest.from_sources(
            self.transaction_request,
            self.application_plan,
            self.verification_result,
            self.preflight_result,
            self.transaction_policy,
            self.reconciliation_policy,
            requested_by=self.request.requested_by,
            requested_at=self.request.requested_at,
        )
        return ArtifactTransactionReconciliation(
            request,
            self.transaction_request,
            self.application_plan,
            self.verification_result,
            self.preflight_result,
            self.transaction_policy,
            self.reconciliation_policy,
        ).reconcile()

    def _application(self) -> ArtifactTransactionApplication:
        return ArtifactTransactionApplication(
            self.transaction_request,
            self.application_plan,
            self.verification_result,
            self.preflight_result,
            self.transaction_policy,
        )

    def _handle_committed(
        self,
        records: list[ArtifactTransactionLifecycleRecord],
        initial: ArtifactTransactionReconciliationResult,
    ) -> ArtifactTransactionLifecycleResult:
        if not self.policy.verify_committed_state:
            self._append_record(
                records,
                phase=(
                    ArtifactTransactionLifecyclePhase.COMMITTED_VERIFICATION
                ),
                status=ArtifactTransactionLifecycleRecordStatus.DEFERRED,
                state_before=ArtifactTransactionLifecycleState.COMMITTED,
                state_after=ArtifactTransactionLifecycleState.COMMITTED,
                component_id=self.transaction_request.transaction_id,
                component_result_hash=None,
                reason=(
                    "DEFERRED: committed receipt verification was disabled "
                    "by lifecycle policy"
                ),
            )
            receipt = self._application().apply()
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.COMMITTED,
                route=ArtifactTransactionLifecycleRoute.VERIFY_COMMITTED,
                initial=initial,
                final_reconciliation=initial,
                transaction_result=receipt,
                recovery_result=None,
                reason="COMMITTED: durable transaction state is present",
            )
        receipt = self._application().apply()
        receipt_hash = receipt.result_hash
        assert receipt_hash is not None
        if receipt.status is not ArtifactTransactionStatus.COMMITTED:
            self._append_record(
                records,
                phase=(
                    ArtifactTransactionLifecyclePhase.COMMITTED_VERIFICATION
                ),
                status=ArtifactTransactionLifecycleRecordStatus.FAILED,
                state_before=ArtifactTransactionLifecycleState.COMMITTED,
                state_after=ArtifactTransactionLifecycleState.FAILED,
                component_id=receipt.transaction_id,
                component_result_hash=receipt_hash,
                reason=(
                    "FAILED: committed reconciliation could not be verified "
                    "by idempotent transaction replay"
                ),
            )
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.FAILED,
                route=ArtifactTransactionLifecycleRoute.VERIFY_COMMITTED,
                initial=initial,
                final_reconciliation=initial,
                transaction_result=receipt,
                recovery_result=None,
                reason="FAILED: committed transaction verification failed",
            )
        self._append_record(
            records,
            phase=ArtifactTransactionLifecyclePhase.COMMITTED_VERIFICATION,
            status=ArtifactTransactionLifecycleRecordStatus.COMPLETED,
            state_before=ArtifactTransactionLifecycleState.COMMITTED,
            state_after=ArtifactTransactionLifecycleState.COMMITTED,
            component_id=receipt.transaction_id,
            component_result_hash=receipt_hash,
            reason=(
                "COMPLETED: committed receipt and final destinations were "
                "verified idempotently"
            ),
        )
        return self._result(
            records=records,
            final_state=ArtifactTransactionLifecycleState.COMMITTED,
            route=ArtifactTransactionLifecycleRoute.VERIFY_COMMITTED,
            initial=initial,
            final_reconciliation=initial,
            transaction_result=receipt,
            recovery_result=None,
            reason="COMMITTED: transaction is durably committed and verified",
        )

    def _apply(
        self,
        records: list[ArtifactTransactionLifecycleRecord],
        initial: ArtifactTransactionReconciliationResult,
        *,
        route: ArtifactTransactionLifecycleRoute,
        recovery_result: ArtifactTransactionRecoveryResult | None = None,
    ) -> ArtifactTransactionLifecycleResult:
        transaction = self._application().apply()
        transaction_hash = transaction.result_hash
        assert transaction_hash is not None
        if transaction.status is ArtifactTransactionStatus.COMMITTED:
            after = ArtifactTransactionLifecycleState.COMMITTED
            status = ArtifactTransactionLifecycleRecordStatus.COMPLETED
            reason = (
                "COMPLETED: low-level transaction application committed all "
                "artifacts and wrote a durable receipt"
            )
        else:
            after = ArtifactTransactionLifecycleState.FAILED
            status = ArtifactTransactionLifecycleRecordStatus.FAILED
            reason = (
                "FAILED: low-level transaction application did not commit"
            )
        self._append_record(
            records,
            phase=ArtifactTransactionLifecyclePhase.APPLICATION,
            status=status,
            state_before=(
                ArtifactTransactionLifecycleState.RECOVERED
                if recovery_result is not None
                else ArtifactTransactionLifecycleState.CLEAN
            ),
            state_after=after,
            component_id=transaction.transaction_id,
            component_result_hash=transaction_hash,
            reason=reason,
        )
        return self._result(
            records=records,
            final_state=after,
            route=route,
            initial=initial,
            final_reconciliation=None,
            transaction_result=transaction,
            recovery_result=recovery_result,
            reason=(
                "COMMITTED: lifecycle application completed"
                if after is ArtifactTransactionLifecycleState.COMMITTED
                else "FAILED: lifecycle application failed"
            ),
        )

    def _recover(
        self,
        records: list[ArtifactTransactionLifecycleRecord],
        initial: ArtifactTransactionReconciliationResult,
    ) -> ArtifactTransactionLifecycleResult:
        recovery_request = ArtifactTransactionRecoveryRequest.from_sources(
            initial,
            self.transaction_request,
            self.transaction_policy,
            self.application_plan,
            self.verification_result,
            self.preflight_result,
            self.recovery_policy,
            requested_by=self.request.requested_by,
            requested_at=self.request.requested_at,
        )
        recovery = ArtifactTransactionRecoveryExecution(
            recovery_request,
            initial,
            self.transaction_request,
            self.transaction_policy,
            self.application_plan,
            self.verification_result,
            self.preflight_result,
            self.recovery_policy,
        ).execute()
        recovery_hash = recovery.result_hash
        assert recovery_hash is not None
        if recovery.status not in {
            ArtifactTransactionRecoveryStatus.COMPLETED,
            ArtifactTransactionRecoveryStatus.NOOP,
        }:
            self._append_record(
                records,
                phase=ArtifactTransactionLifecyclePhase.RECOVERY,
                status=ArtifactTransactionLifecycleRecordStatus.FAILED,
                state_before=(
                    ArtifactTransactionLifecycleState.RECOVERY_REQUIRED
                ),
                state_after=ArtifactTransactionLifecycleState.FAILED,
                component_id=recovery.recovery_id,
                component_result_hash=recovery_hash,
                reason="FAILED: low-level recovery did not complete",
            )
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.FAILED,
                route=ArtifactTransactionLifecycleRoute.RECOVER,
                initial=initial,
                final_reconciliation=None,
                transaction_result=None,
                recovery_result=recovery,
                reason="FAILED: lifecycle recovery failed",
            )
        self._append_record(
            records,
            phase=ArtifactTransactionLifecyclePhase.RECOVERY,
            status=ArtifactTransactionLifecycleRecordStatus.COMPLETED,
            state_before=ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
            state_after=ArtifactTransactionLifecycleState.RECOVERED,
            component_id=recovery.recovery_id,
            component_result_hash=recovery_hash,
            reason=(
                "COMPLETED: low-level recovery applied the deterministic "
                "reconciliation plan"
            ),
        )
        final_reconciliation = self._reconcile()
        final_hash = final_reconciliation.result_hash
        assert final_hash is not None
        observed_state = self._state_from_reconciliation(
            final_reconciliation
        )
        final_state = (
            ArtifactTransactionLifecycleState.RECOVERED
            if final_reconciliation.status
            is ArtifactTransactionReconciliationStatus.CLEAN
            else observed_state
        )
        self._append_record(
            records,
            phase=(
                ArtifactTransactionLifecyclePhase.POST_RECOVERY_RECONCILE
            ),
            status=(
                ArtifactTransactionLifecycleRecordStatus.COMPLETED
                if final_reconciliation.status
                is not ArtifactTransactionReconciliationStatus.CONFLICTED
                else ArtifactTransactionLifecycleRecordStatus.REFUSED
            ),
            state_before=ArtifactTransactionLifecycleState.RECOVERED,
            state_after=final_state,
            component_id=final_reconciliation.reconciliation_id,
            component_result_hash=final_hash,
            reason=(
                "COMPLETED: post-recovery reconciliation verified the new "
                "transaction state"
            ),
        )
        if (
            final_reconciliation.status
            is ArtifactTransactionReconciliationStatus.COMMITTED
        ):
            receipt = self._application().apply()
            receipt_hash = receipt.result_hash
            assert receipt_hash is not None
            self._append_record(
                records,
                phase=(
                    ArtifactTransactionLifecyclePhase.COMMITTED_VERIFICATION
                ),
                status=ArtifactTransactionLifecycleRecordStatus.COMPLETED,
                state_before=ArtifactTransactionLifecycleState.COMMITTED,
                state_after=ArtifactTransactionLifecycleState.COMMITTED,
                component_id=receipt.transaction_id,
                component_result_hash=receipt_hash,
                reason=(
                    "COMPLETED: finalized transaction receipt was verified "
                    "idempotently"
                ),
            )
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.COMMITTED,
                route=ArtifactTransactionLifecycleRoute.RECOVER,
                initial=initial,
                final_reconciliation=final_reconciliation,
                transaction_result=receipt,
                recovery_result=recovery,
                reason=(
                    "COMMITTED: recovery finalized and verified the transaction"
                ),
            )
        if (
            final_reconciliation.status
            is ArtifactTransactionReconciliationStatus.CLEAN
        ):
            if self.policy.apply_after_recovery:
                return self._apply(
                    records,
                    initial,
                    route=(
                        ArtifactTransactionLifecycleRoute.RECOVER_THEN_APPLY
                    ),
                    recovery_result=recovery,
                )
            return self._result(
                records=records,
                final_state=ArtifactTransactionLifecycleState.RECOVERED,
                route=ArtifactTransactionLifecycleRoute.RECOVER,
                initial=initial,
                final_reconciliation=final_reconciliation,
                transaction_result=None,
                recovery_result=recovery,
                reason=(
                    "RECOVERED: transaction returned to a clean pre-application "
                    "state"
                ),
            )
        return self._result(
            records=records,
            final_state=(
                ArtifactTransactionLifecycleState.CONFLICTED
                if final_reconciliation.status
                is ArtifactTransactionReconciliationStatus.CONFLICTED
                else ArtifactTransactionLifecycleState.FAILED
            ),
            route=(
                ArtifactTransactionLifecycleRoute.REFUSE
                if final_reconciliation.status
                is ArtifactTransactionReconciliationStatus.CONFLICTED
                else ArtifactTransactionLifecycleRoute.RECOVER
            ),
            initial=initial,
            final_reconciliation=final_reconciliation,
            transaction_result=None,
            recovery_result=recovery,
            reason=(
                "REFUSED: recovery produced a conflicted state"
                if final_reconciliation.status
                is ArtifactTransactionReconciliationStatus.CONFLICTED
                else "FAILED: recovery did not converge to clean or committed"
            ),
        )

    def _append_record(
        self,
        records: list[ArtifactTransactionLifecycleRecord],
        *,
        phase: ArtifactTransactionLifecyclePhase,
        status: ArtifactTransactionLifecycleRecordStatus,
        state_before: ArtifactTransactionLifecycleState,
        state_after: ArtifactTransactionLifecycleState,
        component_id: str,
        component_result_hash: str | None,
        reason: str,
    ) -> None:
        if len(records) >= self.policy.max_transitions:
            raise ArtifactTransactionLifecycleError(
                "lifecycle transition count exceeds policy maximum"
            )
        records.append(
            ArtifactTransactionLifecycleRecord(
                index=len(records),
                phase=phase,
                status=status,
                state_before=state_before,
                state_after=state_after,
                component_id=component_id,
                component_result_hash=component_result_hash,
                reason=reason,
            )
        )

    @staticmethod
    def _state_from_reconciliation(
        result: ArtifactTransactionReconciliationResult,
    ) -> ArtifactTransactionLifecycleState:
        mapping = {
            ArtifactTransactionReconciliationStatus.CLEAN: (
                ArtifactTransactionLifecycleState.CLEAN
            ),
            ArtifactTransactionReconciliationStatus.COMMITTED: (
                ArtifactTransactionLifecycleState.COMMITTED
            ),
            ArtifactTransactionReconciliationStatus.RECOVERABLE: (
                ArtifactTransactionLifecycleState.RECOVERY_REQUIRED
            ),
            ArtifactTransactionReconciliationStatus.CONFLICTED: (
                ArtifactTransactionLifecycleState.CONFLICTED
            ),
        }
        return mapping[result.status]

    def _result(
        self,
        *,
        records: list[ArtifactTransactionLifecycleRecord],
        final_state: ArtifactTransactionLifecycleState,
        route: ArtifactTransactionLifecycleRoute,
        initial: ArtifactTransactionReconciliationResult,
        final_reconciliation: (
            ArtifactTransactionReconciliationResult | None
        ),
        transaction_result: ArtifactTransactionResult | None,
        recovery_result: ArtifactTransactionRecoveryResult | None,
        reason: str,
    ) -> ArtifactTransactionLifecycleResult:
        request_hash = self.request.request_hash
        initial_hash = initial.result_hash
        assert request_hash is not None
        assert initial_hash is not None
        transaction_hash = (
            transaction_result.result_hash
            if transaction_result is not None
            else None
        )
        recovery_hash = (
            recovery_result.result_hash
            if recovery_result is not None
            else None
        )
        final_reconciliation_hash = (
            final_reconciliation.result_hash
            if final_reconciliation is not None
            else None
        )
        return ArtifactTransactionLifecycleResult(
            lifecycle_id=self.request.lifecycle_id,
            final_state=final_state,
            route=route,
            request_hash=request_hash,
            policy_id=self.policy.policy_id,
            policy_hash=self.policy.policy_hash,
            transaction_id=self.transaction_request.transaction_id,
            transaction_request_hash=(
                self.request.transaction_request_hash
            ),
            application_plan_hash=self.request.application_plan_hash,
            verification_result_hash=(
                self.request.verification_result_hash
            ),
            preflight_result_hash=self.request.preflight_result_hash,
            workspace_root=self.request.workspace_root,
            records=tuple(records),
            initial_reconciliation_result_hash=initial_hash,
            final_reconciliation_result_hash=final_reconciliation_hash,
            transaction_result_hash=transaction_hash,
            recovery_result_hash=recovery_hash,
            transition_count=len(records),
            completed_at=self.request.requested_at,
            reason=reason,
        )
