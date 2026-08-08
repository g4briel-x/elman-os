"""Read-only ELMAN Studio projection for the v0.7 orchestration contracts.

The module turns one integrity-checked final-verification request and its
optional signed report into a deterministic dashboard snapshot.  It does not
execute agents, mutate a plan, approve an operation, write project artifacts,
read environment variables, or access the network.

The optional Flet entry point is deliberately read-only.  Completion is shown
as authorized only when the final report is both successful and verified with
the caller-supplied HMAC signer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .agent_contracts import canonical_json
from .execution_plan import StepStatus
from .final_verification import (
    FinalReportSignatureError,
    FinalReportSigner,
    FinalVerificationGate,
    FinalVerificationReport,
    FinalVerificationRequest,
    FinalVerificationStatus,
)
from .project_memory import ProjectMemoryKind


STUDIO_V07_SNAPSHOT_VERSION: Final[int] = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StudioV07Error(ValueError):
    """The Studio v0.7 projection or invocation is invalid."""


class StudioV07IntegrityError(StudioV07Error):
    """A request, report, or snapshot failed an integrity binding."""


class StudioFinalState(StrEnum):
    NOT_RUN = "not-run"
    SIGNATURE_UNVERIFIED = "signature-unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class StudioApprovalState(StrEnum):
    REQUIRED = "required"
    GRANTED = "granted"


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StudioV07Error(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise StudioV07Error(f"{name} exceeds {maximum} characters")
    if any(
        ord(character) < 32 and character not in "\n\r\t"
        for character in result
    ):
        raise StudioV07Error(f"{name} contains control characters")
    return result


def _identifier(value: object, name: str) -> str:
    result = _text(value, name, maximum=192)
    if _IDENTIFIER.fullmatch(result) is None:
        raise StudioV07Error(f"{name} has an invalid format")
    return result


def _hash(value: object, name: str) -> str:
    result = _text(value, name, maximum=64)
    if _SHA256.fullmatch(result) is None:
        raise StudioV07Error(f"{name} must be a lowercase SHA-256 digest")
    return result


def _optional_hash(value: object | None, name: str) -> str | None:
    return None if value is None else _hash(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise StudioV07Error(f"{name} must be boolean")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StudioV07Error(f"{name} must be a non-negative integer")
    return value


def _ratio(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudioV07Error(f"{name} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise StudioV07Error(f"{name} must be between 0 and 1")
    return result


def _sha256_document(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _normalize_cards(
    values: Iterable[Any],
    expected_type: type,
    key: str,
    name: str,
) -> tuple[Any, ...]:
    cards = tuple(values)
    if not all(isinstance(item, expected_type) for item in cards):
        raise StudioV07Error(
            f"{name} must contain {expected_type.__name__} values"
        )
    keys = [getattr(item, key) for item in cards]
    if len(keys) != len(set(keys)):
        raise StudioV07IntegrityError(f"{name} contains duplicate identifiers")
    return tuple(sorted(cards, key=lambda item: getattr(item, key)))


@dataclass(frozen=True, slots=True)
class StudioStepCard:
    step_id: str
    title: str
    capability_id: str
    status: str
    assigned_agent_id: str | None
    dependencies: tuple[str, ...]
    progress: float
    requires_human_approval: bool
    approval_reference: str | None
    last_event_at: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "step_id"))
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, "capability_id"),
        )
        try:
            status = StepStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise StudioV07Error("step status is invalid") from exc
        object.__setattr__(self, "status", status.value)
        if self.assigned_agent_id is not None:
            object.__setattr__(
                self,
                "assigned_agent_id",
                _identifier(self.assigned_agent_id, "assigned_agent_id"),
            )
        dependencies = tuple(sorted({_identifier(item, "dependency") for item in self.dependencies}))
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "progress", _ratio(self.progress, "progress"))
        object.__setattr__(
            self,
            "requires_human_approval",
            _boolean(self.requires_human_approval, "requires_human_approval"),
        )
        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _identifier(self.approval_reference, "approval_reference"),
            )
        if self.last_event_at is not None:
            object.__setattr__(
                self,
                "last_event_at",
                _text(self.last_event_at, "last_event_at", maximum=64),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "capability_id": self.capability_id,
            "status": self.status,
            "assigned_agent_id": self.assigned_agent_id,
            "dependencies": list(self.dependencies),
            "progress": self.progress,
            "requires_human_approval": self.requires_human_approval,
            "approval_reference": self.approval_reference,
            "last_event_at": self.last_event_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioStepCard":
        return cls(
            step_id=data["step_id"],
            title=data["title"],
            capability_id=data["capability_id"],
            status=data["status"],
            assigned_agent_id=data.get("assigned_agent_id"),
            dependencies=tuple(data.get("dependencies", ())),
            progress=data["progress"],
            requires_human_approval=data["requires_human_approval"],
            approval_reference=data.get("approval_reference"),
            last_event_at=data.get("last_event_at"),
        )


@dataclass(frozen=True, slots=True)
class StudioAgentCard:
    agent_id: str
    step_ids: tuple[str, ...]
    capability_ids: tuple[str, ...]
    active_step_count: int
    failed_step_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _identifier(self.agent_id, "agent_id"))
        object.__setattr__(
            self,
            "step_ids",
            tuple(sorted({_identifier(item, "step_id") for item in self.step_ids})),
        )
        object.__setattr__(
            self,
            "capability_ids",
            tuple(
                sorted(
                    {_identifier(item, "capability_id") for item in self.capability_ids}
                )
            ),
        )
        object.__setattr__(
            self,
            "active_step_count",
            _non_negative_int(self.active_step_count, "active_step_count"),
        )
        object.__setattr__(
            self,
            "failed_step_count",
            _non_negative_int(self.failed_step_count, "failed_step_count"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "step_ids": list(self.step_ids),
            "capability_ids": list(self.capability_ids),
            "active_step_count": self.active_step_count,
            "failed_step_count": self.failed_step_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioAgentCard":
        return cls(
            agent_id=data["agent_id"],
            step_ids=tuple(data.get("step_ids", ())),
            capability_ids=tuple(data.get("capability_ids", ())),
            active_step_count=data["active_step_count"],
            failed_step_count=data["failed_step_count"],
        )


@dataclass(frozen=True, slots=True)
class StudioApprovalCard:
    approval_id: str
    scope: str
    subject_id: str
    state: StudioApprovalState
    reference: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "approval_id", _identifier(self.approval_id, "approval_id")
        )
        object.__setattr__(self, "scope", _identifier(self.scope, "scope"))
        object.__setattr__(
            self, "subject_id", _identifier(self.subject_id, "subject_id")
        )
        try:
            state = StudioApprovalState(self.state)
        except (TypeError, ValueError) as exc:
            raise StudioV07Error("approval state is invalid") from exc
        object.__setattr__(self, "state", state)
        if self.reference is not None:
            object.__setattr__(
                self, "reference", _identifier(self.reference, "reference")
            )
        if (state is StudioApprovalState.GRANTED) != (self.reference is not None):
            raise StudioV07IntegrityError(
                "granted approvals require a reference and required approvals forbid one"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "scope": self.scope,
            "subject_id": self.subject_id,
            "state": self.state.value,
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioApprovalCard":
        return cls(
            approval_id=data["approval_id"],
            scope=data["scope"],
            subject_id=data["subject_id"],
            state=StudioApprovalState(data["state"]),
            reference=data.get("reference"),
        )


@dataclass(frozen=True, slots=True)
class StudioMemoryCard:
    memory_id: str
    kind: str
    state: str
    revision: int
    revision_count: int
    title: str
    payload_available: bool
    payload_hash: str
    revision_hash: str
    origin_type: str
    origin_reference: str
    decision_link_state: str
    decision_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "memory_id", _identifier(self.memory_id, "memory_id"))
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        object.__setattr__(self, "state", _identifier(self.state, "state"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise StudioV07Error("revision must be a positive integer")
        if (
            isinstance(self.revision_count, bool)
            or not isinstance(self.revision_count, int)
            or self.revision_count < 1
        ):
            raise StudioV07Error("revision_count must be a positive integer")
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(
            self,
            "payload_available",
            _boolean(self.payload_available, "payload_available"),
        )
        object.__setattr__(self, "payload_hash", _hash(self.payload_hash, "payload_hash"))
        object.__setattr__(
            self, "revision_hash", _hash(self.revision_hash, "revision_hash")
        )
        object.__setattr__(
            self, "origin_type", _identifier(self.origin_type, "origin_type")
        )
        object.__setattr__(
            self,
            "origin_reference",
            _identifier(self.origin_reference, "origin_reference"),
        )
        if self.decision_link_state not in {"not-applicable", "missing", "coherent", "incoherent"}:
            raise StudioV07Error("decision_link_state is invalid")
        object.__setattr__(
            self,
            "decision_evidence_ids",
            tuple(
                sorted(
                    {
                        _identifier(item, "decision_evidence_id")
                        for item in self.decision_evidence_ids
                    }
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "state": self.state,
            "revision": self.revision,
            "revision_count": self.revision_count,
            "title": self.title,
            "payload_available": self.payload_available,
            "payload_hash": self.payload_hash,
            "revision_hash": self.revision_hash,
            "origin_type": self.origin_type,
            "origin_reference": self.origin_reference,
            "decision_link_state": self.decision_link_state,
            "decision_evidence_ids": list(self.decision_evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioMemoryCard":
        return cls(
            memory_id=data["memory_id"],
            kind=data["kind"],
            state=data["state"],
            revision=data["revision"],
            revision_count=data["revision_count"],
            title=data["title"],
            payload_available=data["payload_available"],
            payload_hash=data["payload_hash"],
            revision_hash=data["revision_hash"],
            origin_type=data["origin_type"],
            origin_reference=data["origin_reference"],
            decision_link_state=data["decision_link_state"],
            decision_evidence_ids=tuple(data.get("decision_evidence_ids", ())),
        )


@dataclass(frozen=True, slots=True)
class StudioEvidenceCard:
    evidence_id: str
    kind: str
    status: str
    step_id: str | None
    source_reference: str
    source_hash: str
    captured_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_id", _identifier(self.evidence_id, "evidence_id")
        )
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        object.__setattr__(self, "status", _identifier(self.status, "status"))
        if self.step_id is not None:
            object.__setattr__(self, "step_id", _identifier(self.step_id, "step_id"))
        object.__setattr__(
            self,
            "source_reference",
            _identifier(self.source_reference, "source_reference"),
        )
        object.__setattr__(self, "source_hash", _hash(self.source_hash, "source_hash"))
        object.__setattr__(
            self,
            "captured_at",
            _text(self.captured_at, "captured_at", maximum=64),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "status": self.status,
            "step_id": self.step_id,
            "source_reference": self.source_reference,
            "source_hash": self.source_hash,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioEvidenceCard":
        return cls(
            evidence_id=data["evidence_id"],
            kind=data["kind"],
            status=data["status"],
            step_id=data.get("step_id"),
            source_reference=data["source_reference"],
            source_hash=data["source_hash"],
            captured_at=data["captured_at"],
        )


@dataclass(frozen=True, slots=True)
class StudioIssueCard:
    issue_id: str
    source: str
    code: str
    summary: str
    resolved: bool
    step_id: str | None
    evidence_reference: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", _identifier(self.issue_id, "issue_id"))
        object.__setattr__(self, "source", _identifier(self.source, "source"))
        object.__setattr__(self, "code", _identifier(self.code, "code"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(self, "resolved", _boolean(self.resolved, "resolved"))
        if self.step_id is not None:
            object.__setattr__(self, "step_id", _identifier(self.step_id, "step_id"))
        if self.evidence_reference is not None:
            object.__setattr__(
                self,
                "evidence_reference",
                _identifier(self.evidence_reference, "evidence_reference"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "source": self.source,
            "code": self.code,
            "summary": self.summary,
            "resolved": self.resolved,
            "step_id": self.step_id,
            "evidence_reference": self.evidence_reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioIssueCard":
        return cls(
            issue_id=data["issue_id"],
            source=data["source"],
            code=data["code"],
            summary=data["summary"],
            resolved=data["resolved"],
            step_id=data.get("step_id"),
            evidence_reference=data.get("evidence_reference"),
        )


@dataclass(frozen=True, slots=True)
class StudioSupervisionCard:
    decision_id: str
    action: str
    confidence_bp: int
    highest_risk: str
    finding_count: int
    approval_required: bool
    approval_reference: str | None
    decided_by: str
    decided_at: str
    rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_id", _identifier(self.decision_id, "decision_id")
        )
        object.__setattr__(self, "action", _identifier(self.action, "action"))
        if (
            isinstance(self.confidence_bp, bool)
            or not isinstance(self.confidence_bp, int)
            or not 0 <= self.confidence_bp <= 10_000
        ):
            raise StudioV07Error("confidence_bp must be between 0 and 10000")
        object.__setattr__(
            self, "highest_risk", _identifier(self.highest_risk, "highest_risk")
        )
        object.__setattr__(
            self,
            "finding_count",
            _non_negative_int(self.finding_count, "finding_count"),
        )
        object.__setattr__(
            self,
            "approval_required",
            _boolean(self.approval_required, "approval_required"),
        )
        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _identifier(self.approval_reference, "approval_reference"),
            )
        object.__setattr__(
            self, "decided_by", _identifier(self.decided_by, "decided_by")
        )
        object.__setattr__(
            self, "decided_at", _text(self.decided_at, "decided_at", maximum=64)
        )
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action,
            "confidence_bp": self.confidence_bp,
            "highest_risk": self.highest_risk,
            "finding_count": self.finding_count,
            "approval_required": self.approval_required,
            "approval_reference": self.approval_reference,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioSupervisionCard":
        return cls(
            decision_id=data["decision_id"],
            action=data["action"],
            confidence_bp=data["confidence_bp"],
            highest_risk=data["highest_risk"],
            finding_count=data["finding_count"],
            approval_required=data["approval_required"],
            approval_reference=data.get("approval_reference"),
            decided_by=data["decided_by"],
            decided_at=data["decided_at"],
            rationale=data["rationale"],
        )


@dataclass(frozen=True, slots=True)
class StudioGateCard:
    gate_id: str
    passed: bool
    checked_count: int
    issue_codes: tuple[str, ...]
    references: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_id", _identifier(self.gate_id, "gate_id"))
        object.__setattr__(self, "passed", _boolean(self.passed, "passed"))
        object.__setattr__(
            self,
            "checked_count",
            _non_negative_int(self.checked_count, "checked_count"),
        )
        object.__setattr__(
            self,
            "issue_codes",
            tuple(sorted({_identifier(item, "issue_code") for item in self.issue_codes})),
        )
        object.__setattr__(
            self,
            "references",
            tuple(sorted({_identifier(item, "reference") for item in self.references})),
        )
        if self.passed == bool(self.issue_codes):
            raise StudioV07IntegrityError(
                "passed gates cannot contain issues and failed gates require issues"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "passed": self.passed,
            "checked_count": self.checked_count,
            "issue_codes": list(self.issue_codes),
            "references": list(self.references),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioGateCard":
        return cls(
            gate_id=data["gate_id"],
            passed=data["passed"],
            checked_count=data["checked_count"],
            issue_codes=tuple(data.get("issue_codes", ())),
            references=tuple(data.get("references", ())),
        )


@dataclass(frozen=True, slots=True)
class StudioDashboardSnapshot:
    snapshot_id: str
    verification_id: str
    request_hash: str
    project_id: str
    plan_id: str
    objective: str
    plan_status: str
    journal_event_count: int
    journal_hash: str
    requested_at: str
    steps: tuple[StudioStepCard, ...]
    agents: tuple[StudioAgentCard, ...]
    approvals: tuple[StudioApprovalCard, ...]
    memory: tuple[StudioMemoryCard, ...]
    evidence: tuple[StudioEvidenceCard, ...]
    issues: tuple[StudioIssueCard, ...]
    supervision: tuple[StudioSupervisionCard, ...]
    gates: tuple[StudioGateCard, ...]
    final_state: StudioFinalState
    report_hash: str | None
    report_key_id: str | None
    signature_verified: bool
    snapshot_hash: str | None = None
    version: int = STUDIO_V07_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "verification_id", "project_id", "plan_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "request_hash", _hash(self.request_hash, "request_hash"))
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        object.__setattr__(
            self, "plan_status", _identifier(self.plan_status, "plan_status")
        )
        object.__setattr__(
            self,
            "journal_event_count",
            _non_negative_int(self.journal_event_count, "journal_event_count"),
        )
        object.__setattr__(self, "journal_hash", _hash(self.journal_hash, "journal_hash"))
        object.__setattr__(
            self, "requested_at", _text(self.requested_at, "requested_at", maximum=64)
        )
        card_specs = (
            ("steps", StudioStepCard, "step_id"),
            ("agents", StudioAgentCard, "agent_id"),
            ("approvals", StudioApprovalCard, "approval_id"),
            ("memory", StudioMemoryCard, "memory_id"),
            ("evidence", StudioEvidenceCard, "evidence_id"),
            ("issues", StudioIssueCard, "issue_id"),
            ("supervision", StudioSupervisionCard, "decision_id"),
            ("gates", StudioGateCard, "gate_id"),
        )
        for name, expected_type, key in card_specs:
            object.__setattr__(
                self,
                name,
                _normalize_cards(getattr(self, name), expected_type, key, name),
            )
        try:
            final_state = StudioFinalState(self.final_state)
        except (TypeError, ValueError) as exc:
            raise StudioV07Error("final_state is invalid") from exc
        object.__setattr__(self, "final_state", final_state)
        object.__setattr__(
            self, "report_hash", _optional_hash(self.report_hash, "report_hash")
        )
        if self.report_key_id is not None:
            object.__setattr__(
                self,
                "report_key_id",
                _identifier(self.report_key_id, "report_key_id"),
            )
        object.__setattr__(
            self,
            "signature_verified",
            _boolean(self.signature_verified, "signature_verified"),
        )
        if self.version != STUDIO_V07_SNAPSHOT_VERSION:
            raise StudioV07Error("unsupported Studio snapshot version")
        self._validate_final_state()
        computed = self.compute_hash()
        if self.snapshot_hash is None:
            object.__setattr__(self, "snapshot_hash", computed)
        elif _hash(self.snapshot_hash, "snapshot_hash") != computed:
            raise StudioV07IntegrityError(
                "snapshot_hash does not match dashboard content"
            )

    def _validate_final_state(self) -> None:
        has_report = self.report_hash is not None
        if not has_report:
            if self.final_state is not StudioFinalState.NOT_RUN:
                raise StudioV07IntegrityError("missing report must remain not-run")
            if self.signature_verified or self.report_key_id is not None or self.gates:
                raise StudioV07IntegrityError(
                    "missing report cannot expose signature or gate results"
                )
            return
        if self.report_key_id is None:
            raise StudioV07IntegrityError("report key identity is missing")
        if not self.signature_verified:
            if self.final_state is not StudioFinalState.SIGNATURE_UNVERIFIED:
                raise StudioV07IntegrityError(
                    "unverified signature must fail closed"
                )
        elif self.final_state not in {
            StudioFinalState.VERIFIED,
            StudioFinalState.REJECTED,
        }:
            raise StudioV07IntegrityError(
                "verified signatures require a terminal report state"
            )
        if {item.gate_id for item in self.gates} != {
            item.value for item in FinalVerificationGate
        }:
            raise StudioV07IntegrityError(
                "a final report must expose all verification gates"
            )

    @property
    def completion_authorized(self) -> bool:
        return (
            self.final_state is StudioFinalState.VERIFIED
            and self.signature_verified
            and all(
                item.state is StudioApprovalState.GRANTED
                for item in self.approvals
            )
        )

    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        return sum(item.progress for item in self.steps) / len(self.steps)

    def hash_material(self) -> dict[str, Any]:
        return {
            "record_type": "studio_v07_dashboard_snapshot",
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "verification_id": self.verification_id,
            "request_hash": self.request_hash,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "objective": self.objective,
            "plan_status": self.plan_status,
            "journal_event_count": self.journal_event_count,
            "journal_hash": self.journal_hash,
            "requested_at": self.requested_at,
            "steps": [item.to_dict() for item in self.steps],
            "agents": [item.to_dict() for item in self.agents],
            "approvals": [item.to_dict() for item in self.approvals],
            "memory": [item.to_dict() for item in self.memory],
            "evidence": [item.to_dict() for item in self.evidence],
            "issues": [item.to_dict() for item in self.issues],
            "supervision": [item.to_dict() for item in self.supervision],
            "gates": [item.to_dict() for item in self.gates],
            "final_state": self.final_state.value,
            "report_hash": self.report_hash,
            "report_key_id": self.report_key_id,
            "signature_verified": self.signature_verified,
        }

    def compute_hash(self) -> str:
        return _sha256_document(self.hash_material())

    def verify_hash(self) -> None:
        if self.snapshot_hash != self.compute_hash():
            raise StudioV07IntegrityError(
                "snapshot_hash does not match dashboard content"
            )

    def to_dict(self) -> dict[str, Any]:
        data = self.hash_material()
        data["snapshot_hash"] = self.snapshot_hash
        data["completion_authorized"] = self.completion_authorized
        data["progress"] = self.progress
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioDashboardSnapshot":
        if data.get("record_type") != "studio_v07_dashboard_snapshot":
            raise StudioV07Error(
                "record_type must be studio_v07_dashboard_snapshot"
            )
        if "snapshot_hash" not in data:
            raise StudioV07IntegrityError("serialized snapshot is missing snapshot_hash")
        snapshot = cls(
            snapshot_id=data["snapshot_id"],
            verification_id=data["verification_id"],
            request_hash=data["request_hash"],
            project_id=data["project_id"],
            plan_id=data["plan_id"],
            objective=data["objective"],
            plan_status=data["plan_status"],
            journal_event_count=data["journal_event_count"],
            journal_hash=data["journal_hash"],
            requested_at=data["requested_at"],
            steps=tuple(StudioStepCard.from_dict(item) for item in data["steps"]),
            agents=tuple(StudioAgentCard.from_dict(item) for item in data["agents"]),
            approvals=tuple(
                StudioApprovalCard.from_dict(item) for item in data["approvals"]
            ),
            memory=tuple(StudioMemoryCard.from_dict(item) for item in data["memory"]),
            evidence=tuple(
                StudioEvidenceCard.from_dict(item) for item in data["evidence"]
            ),
            issues=tuple(StudioIssueCard.from_dict(item) for item in data["issues"]),
            supervision=tuple(
                StudioSupervisionCard.from_dict(item)
                for item in data["supervision"]
            ),
            gates=tuple(StudioGateCard.from_dict(item) for item in data["gates"]),
            final_state=StudioFinalState(data["final_state"]),
            report_hash=data.get("report_hash"),
            report_key_id=data.get("report_key_id"),
            signature_verified=data["signature_verified"],
            snapshot_hash=data["snapshot_hash"],
            version=data.get("version", 0),
        )
        if "completion_authorized" in data and data["completion_authorized"] is not snapshot.completion_authorized:
            raise StudioV07IntegrityError(
                "completion_authorized does not match snapshot state"
            )
        if "progress" in data and abs(float(data["progress"]) - snapshot.progress) > 1e-12:
            raise StudioV07IntegrityError("progress does not match step state")
        return snapshot

    @classmethod
    def from_json(cls, payload: str) -> "StudioDashboardSnapshot":
        try:
            data = json.loads(_text(payload, "snapshot_json", maximum=64_000_000))
        except json.JSONDecodeError as exc:
            raise StudioV07Error("snapshot JSON is invalid") from exc
        if not isinstance(data, dict):
            raise StudioV07Error("snapshot JSON must contain an object")
        return cls.from_dict(data)


_STEP_PROGRESS: Final[dict[StepStatus, float]] = {
    StepStatus.PENDING: 0.0,
    StepStatus.APPROVED: 0.1,
    StepStatus.RUNNING: 0.5,
    StepStatus.BLOCKED: 0.5,
    StepStatus.FAILED: 0.5,
    StepStatus.COMPLETED: 1.0,
}


@dataclass(frozen=True, slots=True)
class StudioV07Projector:
    """Create a deterministic, integrity-bound Studio dashboard snapshot."""

    request: FinalVerificationRequest
    report: FinalVerificationReport | None = None
    signer: FinalReportSigner | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, FinalVerificationRequest):
            raise StudioV07Error(
                "request must be a FinalVerificationRequest"
            )
        if self.report is not None and not isinstance(
            self.report, FinalVerificationReport
        ):
            raise StudioV07Error(
                "report must be a FinalVerificationReport or None"
            )
        if self.signer is not None and not isinstance(self.signer, FinalReportSigner):
            raise StudioV07Error("signer must be a FinalReportSigner or None")
        if self.report is None and self.signer is not None:
            raise StudioV07Error("a signer cannot be used without a report")

    def _verify_sources(self) -> bool:
        try:
            self.request.verify_hash()
        except ValueError as exc:
            raise StudioV07IntegrityError(
                "final-verification request integrity failed"
            ) from exc
        if self.report is None:
            return False
        try:
            self.report.verify_hash()
        except ValueError as exc:
            raise StudioV07IntegrityError(
                "final-verification report integrity failed"
            ) from exc
        expected = {
            "verification_id": self.request.verification_id,
            "request_hash": self.request.request_hash,
            "plan_id": self.request.plan.plan_id,
            "project_id": self.request.plan.project_id,
            "plan_state_hash": self.request.plan_state_hash,
            "journal_hash": self.request.journal_hash,
        }
        mismatched = sorted(
            name
            for name, value in expected.items()
            if getattr(self.report, name) != value
        )
        if mismatched:
            raise StudioV07IntegrityError(
                "report is not bound to the displayed request: "
                + ", ".join(mismatched)
            )
        if self.signer is None:
            return False
        try:
            self.report.verify_signature(self.signer)
        except FinalReportSignatureError as exc:
            raise StudioV07IntegrityError(
                "final report signature verification failed"
            ) from exc
        return True

    def _step_cards(self) -> tuple[StudioStepCard, ...]:
        latest_event: dict[str, str] = {}
        for event in self.request.journal.events:
            if event.step_id is not None:
                latest_event[event.step_id] = event.timestamp
        return tuple(
            StudioStepCard(
                step_id=step.step_id,
                title=step.title,
                capability_id=step.capability_id,
                status=step.status.value,
                assigned_agent_id=step.assigned_agent_id,
                dependencies=step.dependencies,
                progress=_STEP_PROGRESS[step.status],
                requires_human_approval=step.requires_human_approval,
                approval_reference=step.approval_reference,
                last_event_at=latest_event.get(step.step_id),
            )
            for step in self.request.plan.steps
        )

    @staticmethod
    def _agent_cards(steps: Sequence[StudioStepCard]) -> tuple[StudioAgentCard, ...]:
        grouped: dict[str, list[StudioStepCard]] = defaultdict(list)
        for step in steps:
            if step.assigned_agent_id is not None:
                grouped[step.assigned_agent_id].append(step)
        return tuple(
            StudioAgentCard(
                agent_id=agent_id,
                step_ids=tuple(item.step_id for item in cards),
                capability_ids=tuple(item.capability_id for item in cards),
                active_step_count=sum(
                    item.status in {StepStatus.APPROVED.value, StepStatus.RUNNING.value}
                    for item in cards
                ),
                failed_step_count=sum(
                    item.status in {StepStatus.BLOCKED.value, StepStatus.FAILED.value}
                    for item in cards
                ),
            )
            for agent_id, cards in sorted(grouped.items())
        )

    def _approval_cards(self) -> tuple[StudioApprovalCard, ...]:
        plan = self.request.plan
        approvals: list[StudioApprovalCard] = []
        if plan.requires_human_approval:
            approvals.append(
                StudioApprovalCard(
                    approval_id=f"studio-approval:plan:{plan.plan_id}",
                    scope="plan",
                    subject_id=plan.plan_id,
                    state=(
                        StudioApprovalState.GRANTED
                        if plan.approval_reference is not None
                        else StudioApprovalState.REQUIRED
                    ),
                    reference=plan.approval_reference,
                )
            )
        for step in plan.steps:
            if not step.requires_human_approval:
                continue
            reference = step.approval_reference or plan.approval_reference
            approvals.append(
                StudioApprovalCard(
                    approval_id=f"studio-approval:step:{step.step_id}",
                    scope="step",
                    subject_id=step.step_id,
                    state=(
                        StudioApprovalState.GRANTED
                        if reference is not None
                        else StudioApprovalState.REQUIRED
                    ),
                    reference=reference,
                )
            )
        for decision in self.request.supervision_decisions:
            if not decision.approval_required:
                continue
            approvals.append(
                StudioApprovalCard(
                    approval_id=(
                        f"studio-approval:supervision:{decision.decision_id}"
                    ),
                    scope="supervision",
                    subject_id=decision.decision_id,
                    state=(
                        StudioApprovalState.GRANTED
                        if decision.approval_reference is not None
                        else StudioApprovalState.REQUIRED
                    ),
                    reference=decision.approval_reference,
                )
            )
        return tuple(approvals)

    def _memory_cards(self) -> tuple[StudioMemoryCard, ...]:
        revisions: dict[str, list[Any]] = defaultdict(list)
        for record in self.request.memory_records:
            revisions[record.memory_id].append(record)
        links = {item.memory_id: item for item in self.request.decision_links}
        cards: list[StudioMemoryCard] = []
        for memory_id, history in sorted(revisions.items()):
            current = max(history, key=lambda item: item.revision)
            link = links.get(memory_id)
            if current.kind is not ProjectMemoryKind.DECISION:
                link_state = "not-applicable"
                evidence_ids: tuple[str, ...] = ()
            elif link is None:
                link_state = "missing"
                evidence_ids = ()
            else:
                link_state = "coherent" if link.coherent else "incoherent"
                evidence_ids = link.evidence_ids
            cards.append(
                StudioMemoryCard(
                    memory_id=memory_id,
                    kind=current.kind.value,
                    state=current.state.value,
                    revision=current.revision,
                    revision_count=len(history),
                    title=current.title or "Payload purged by retention policy",
                    payload_available=current.payload_available,
                    payload_hash=current.payload_hash,
                    revision_hash=current.revision_hash,
                    origin_type=current.origin.source_type.value,
                    origin_reference=current.origin.source_id,
                    decision_link_state=link_state,
                    decision_evidence_ids=evidence_ids,
                )
            )
        return tuple(cards)

    def _evidence_cards(self) -> tuple[StudioEvidenceCard, ...]:
        return tuple(
            StudioEvidenceCard(
                evidence_id=item.evidence_id,
                kind=item.kind.value,
                status=item.status.value,
                step_id=item.step_id,
                source_reference=item.source_reference,
                source_hash=item.source_hash,
                captured_at=item.captured_at,
            )
            for item in self.request.evidence
        )

    def _issue_cards(self) -> tuple[StudioIssueCard, ...]:
        issues: list[StudioIssueCard] = []
        for finding in self.request.policy_findings:
            issues.append(
                StudioIssueCard(
                    issue_id=f"studio-issue:policy:{finding.finding_id}",
                    source="policy",
                    code=finding.rule_id,
                    summary=finding.summary,
                    resolved=finding.resolved,
                    step_id=None,
                    evidence_reference=finding.resolution_evidence_id,
                )
            )
        for error in self.request.execution_errors:
            issues.append(
                StudioIssueCard(
                    issue_id=f"studio-issue:execution:{error.error_id}",
                    source="execution",
                    code=error.code,
                    summary=error.summary,
                    resolved=error.resolved,
                    step_id=error.step_id,
                    evidence_reference=error.resolution_evidence_id,
                )
            )
        for decision in self.request.supervision_decisions:
            for finding in decision.findings:
                issues.append(
                    StudioIssueCard(
                        issue_id=f"studio-issue:supervision:{finding.finding_id}",
                        source="supervision",
                        code=finding.kind.value,
                        summary=finding.summary,
                        resolved=False,
                        step_id=(
                            finding.affected_step_ids[0]
                            if len(finding.affected_step_ids) == 1
                            else None
                        ),
                        evidence_reference=(
                            finding.evidence_references[0]
                            if len(finding.evidence_references) == 1
                            else None
                        ),
                    )
                )
        known_issue_steps = {
            item.step_id for item in issues if item.step_id is not None
        }
        for step in self.request.plan.steps:
            if step.status not in {StepStatus.BLOCKED, StepStatus.FAILED}:
                continue
            if step.step_id in known_issue_steps:
                continue
            issues.append(
                StudioIssueCard(
                    issue_id=f"studio-issue:plan:{step.step_id}",
                    source="plan",
                    code=f"step.{step.status.value}",
                    summary=f"Step {step.step_id} is {step.status.value}.",
                    resolved=False,
                    step_id=step.step_id,
                    evidence_reference=None,
                )
            )
        return tuple(issues)

    def _supervision_cards(self) -> tuple[StudioSupervisionCard, ...]:
        return tuple(
            StudioSupervisionCard(
                decision_id=item.decision_id,
                action=item.action.value,
                confidence_bp=item.confidence_bp,
                highest_risk=item.highest_risk.value,
                finding_count=len(item.findings),
                approval_required=item.approval_required,
                approval_reference=item.approval_reference,
                decided_by=item.decided_by,
                decided_at=item.decided_at,
                rationale=item.rationale,
            )
            for item in self.request.supervision_decisions
        )

    def _gate_cards(self) -> tuple[StudioGateCard, ...]:
        if self.report is None:
            return ()
        return tuple(
            StudioGateCard(
                gate_id=item.gate.value,
                passed=item.passed,
                checked_count=item.checked_count,
                issue_codes=item.issue_codes,
                references=item.references,
            )
            for item in self.report.gates
        )

    def project(self) -> StudioDashboardSnapshot:
        signature_verified = self._verify_sources()
        steps = self._step_cards()
        if self.report is None:
            final_state = StudioFinalState.NOT_RUN
            report_hash = None
            key_id = None
        elif not signature_verified:
            final_state = StudioFinalState.SIGNATURE_UNVERIFIED
            report_hash = self.report.report_hash
            key_id = self.report.key_id
        else:
            final_state = (
                StudioFinalState.VERIFIED
                if self.report.status is FinalVerificationStatus.VERIFIED
                else StudioFinalState.REJECTED
            )
            report_hash = self.report.report_hash
            key_id = self.report.key_id
        return StudioDashboardSnapshot(
            snapshot_id=f"studio-snapshot:{self.request.verification_id}",
            verification_id=self.request.verification_id,
            request_hash=self.request.request_hash,
            project_id=self.request.plan.project_id,
            plan_id=self.request.plan.plan_id,
            objective=self.request.plan.objective,
            plan_status=self.request.plan.status.value,
            journal_event_count=self.request.journal_event_count,
            journal_hash=self.request.journal_hash,
            requested_at=self.request.requested_at,
            steps=steps,
            agents=self._agent_cards(steps),
            approvals=self._approval_cards(),
            memory=self._memory_cards(),
            evidence=self._evidence_cards(),
            issues=self._issue_cards(),
            supervision=self._supervision_cards(),
            gates=self._gate_cards(),
            final_state=final_state,
            report_hash=report_hash,
            report_key_id=key_id,
            signature_verified=signature_verified,
        )


def load_dashboard_snapshot(
    request_path: str | Path,
    *,
    report_path: str | Path | None = None,
    signer: FinalReportSigner | None = None,
) -> StudioDashboardSnapshot:
    """Load explicit local JSON files and project a read-only dashboard."""

    request_file = Path(request_path).expanduser().resolve()
    if not request_file.is_file():
        raise StudioV07Error(f"request file does not exist: {request_file}")
    try:
        request = FinalVerificationRequest.from_json(
            request_file.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise StudioV07IntegrityError(
            "final-verification request file is invalid"
        ) from exc
    report = None
    if report_path is not None:
        report_file = Path(report_path).expanduser().resolve()
        if not report_file.is_file():
            raise StudioV07Error(f"report file does not exist: {report_file}")
        try:
            report = FinalVerificationReport.from_json(
                report_file.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise StudioV07IntegrityError(
                "final-verification report file is invalid"
            ) from exc
    return StudioV07Projector(request, report, signer).project()


def launch_studio_v07(
    request_path: str | Path,
    *,
    report_path: str | Path | None = None,
    signer: FinalReportSigner | None = None,
) -> None:
    """Launch the optional read-only Flet dashboard for a verified snapshot."""

    snapshot = load_dashboard_snapshot(
        request_path,
        report_path=report_path,
        signer=signer,
    )
    try:
        import flet as ft
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError(
            'Installer Studio avec: python -m pip install -e ".[studio]"'
        ) from exc

    def badge(text: str, ok: bool) -> Any:
        return ft.Container(
            ft.Text(text, color="white", weight=ft.FontWeight.BOLD),
            bgcolor="#137333" if ok else "#B3261E",
            border_radius=12,
            padding=8,
        )

    def card(title: str, lines: Iterable[str]) -> Any:
        return ft.Card(
            content=ft.Container(
                ft.Column(
                    [ft.Text(title, weight=ft.FontWeight.BOLD)]
                    + [ft.Text(line, selectable=True) for line in lines],
                    spacing=4,
                ),
                padding=12,
            )
        )

    def main(page: Any) -> None:  # pragma: no cover - optional integration
        page.title = "ELMAN Studio v0.7"
        page.padding = 24
        page.scroll = ft.ScrollMode.AUTO
        page.window_width = 1280
        page.window_height = 900
        page.add(
            ft.Text("ELMAN Studio v0.7", size=30, weight=ft.FontWeight.BOLD),
            ft.Text(snapshot.objective),
            ft.Row(
                [
                    badge(
                        f"Final: {snapshot.final_state.value}",
                        snapshot.completion_authorized,
                    ),
                    badge(
                        "Signature vérifiée"
                        if snapshot.signature_verified
                        else "Signature non vérifiée",
                        snapshot.signature_verified,
                    ),
                    badge(
                        "Clôture autorisée"
                        if snapshot.completion_authorized
                        else "Clôture refusée",
                        snapshot.completion_authorized,
                    ),
                ],
                wrap=True,
            ),
            ft.ProgressBar(value=snapshot.progress),
            ft.Text(
                f"Plan {snapshot.plan_id} • {snapshot.plan_status} • "
                f"{snapshot.journal_event_count} événements"
            ),
            ft.Divider(),
            ft.Text("Plan et agents", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        f"{item.step_id} — {item.title}",
                        (
                            f"État : {item.status} • progression {item.progress:.0%}",
                            f"Agent : {item.assigned_agent_id or 'non assigné'}",
                            f"Capacité : {item.capability_id}",
                        ),
                    )
                    for item in snapshot.steps
                ]
            ),
            ft.Divider(),
            ft.Text("Approbations", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.subject_id,
                        (
                            f"Portée : {item.scope}",
                            f"État : {item.state.value}",
                            f"Référence : {item.reference or 'absente'}",
                        ),
                    )
                    for item in snapshot.approvals
                ]
            ),
            ft.Divider(),
            ft.Text("Décisions et mémoire", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.title,
                        (
                            f"{item.kind} • {item.state} • révision {item.revision}",
                            f"Origine : {item.origin_type} / {item.origin_reference}",
                            f"Résultat : {item.decision_link_state}",
                        ),
                    )
                    for item in snapshot.memory
                ]
            ),
            ft.Divider(),
            ft.Text("Preuves", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.evidence_id,
                        (
                            f"{item.kind} • {item.status}",
                            f"Étape : {item.step_id or 'globale'}",
                            f"Source : {item.source_reference}",
                        ),
                    )
                    for item in snapshot.evidence
                ]
            ),
            ft.Divider(),
            ft.Text("Erreurs et blocages", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.code,
                        (
                            item.summary,
                            f"Source : {item.source} • résolu : {item.resolved}",
                        ),
                    )
                    for item in snapshot.issues
                ]
                or [ft.Text("Aucun blocage déclaré.")]
            ),
            ft.Divider(),
            ft.Text("Supervision métacognitive", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.decision_id,
                        (
                            f"Action : {item.action} • risque : {item.highest_risk}",
                            f"Confiance : {item.confidence_bp / 100:.2f}%",
                            item.rationale,
                        ),
                    )
                    for item in snapshot.supervision
                ]
            ),
            ft.Divider(),
            ft.Text("Vérification finale", size=22, weight=ft.FontWeight.BOLD),
            ft.Column(
                [
                    card(
                        item.gate_id,
                        (
                            f"Résultat : {'PASS' if item.passed else 'FAIL'}",
                            f"Éléments contrôlés : {item.checked_count}",
                            f"Problèmes : {', '.join(item.issue_codes) or 'aucun'}",
                        ),
                    )
                    for item in snapshot.gates
                ]
                or [ft.Text("La vérification finale n'a pas encore été exécutée.")]
            ),
        )

    ft.app(target=main)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m elman_os.studio_v07",
        description="Launch the read-only ELMAN Studio v0.7 dashboard.",
    )
    parser.add_argument("--request", required=True, help="Final request JSON path")
    parser.add_argument("--report", help="Signed final report JSON path")
    parser.add_argument(
        "--key-file",
        help="Binary HMAC key path; never use raw secret text on the command line",
    )
    parser.add_argument("--key-id", help="Key identifier embedded in the report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if (args.key_file is None) != (args.key_id is None):
        raise StudioV07Error("--key-file and --key-id must be supplied together")
    if args.key_file is not None and args.report is None:
        raise StudioV07Error("--key-file requires --report")
    signer = None
    if args.key_file is not None:
        key_path = Path(args.key_file).expanduser().resolve()
        if not key_path.is_file():
            raise StudioV07Error(f"key file does not exist: {key_path}")
        signer = FinalReportSigner(
            key_id=args.key_id,
            secret=key_path.read_bytes(),
        )
    launch_studio_v07(
        args.request,
        report_path=args.report,
        signer=signer,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual optional entry point
    raise SystemExit(main())
