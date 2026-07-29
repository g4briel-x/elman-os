"""Explicit, auditable human approval gates for sensitive ELMAN-OS actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from .metacognition import redact_sensitive


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


PROTECTED_ACTIONS: frozenset[str] = frozenset(
    {
        "activate_learning_proposal",
        "activate_paid_service",
        "create_or_rotate_secret",
        "delete_resource",
        "destructive_migration",
        "production_deploy",
        "send_external_message",
        "store_publication",
        "use_real_customer_data",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    request_id: str
    action: str
    requested_by: str
    reason: str
    context: dict[str, Any]
    status: ApprovalStatus
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalRepository(Protocol):
    def save_approval(self, record: ApprovalRecord) -> None: ...

    def get_approval(self, request_id: str) -> ApprovalRecord | None: ...


@dataclass(slots=True)
class HumanApprovalGate:
    repository: ApprovalRepository

    def request(
        self,
        *,
        action: str,
        requested_by: str,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        if action not in PROTECTED_ACTIONS:
            raise ValueError(f"Action non protégée ou inconnue: {action}")
        if not requested_by.strip() or not reason.strip():
            raise ValueError("Le demandeur et la raison sont obligatoires")
        record = ApprovalRecord(
            request_id=f"approval-{uuid4().hex[:16]}",
            action=action,
            requested_by=requested_by,
            reason=reason,
            context=redact_sensitive(dict(context or {})),
            status=ApprovalStatus.PENDING,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.repository.save_approval(record)
        return record

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        decided_by: str,
        note: str = "",
    ) -> ApprovalRecord:
        record = self.repository.get_approval(request_id)
        if record is None:
            raise KeyError(f"Demande d'approbation inconnue: {request_id}")
        if record.status != ApprovalStatus.PENDING:
            raise ValueError("Cette demande a déjà été décidée")
        if not decided_by.strip():
            raise ValueError("Le décideur est obligatoire")
        if decided_by == record.requested_by:
            raise PermissionError("Le demandeur ne peut pas approuver sa propre action")
        updated = replace(
            record,
            status=ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
            decided_at=datetime.now(UTC).isoformat(),
            decided_by=decided_by,
            decision_note=note or None,
        )
        self.repository.save_approval(updated)
        return updated

    def require(self, action: str, request_id: str) -> ApprovalRecord:
        record = self.repository.get_approval(request_id)
        if record is None:
            raise PermissionError("Aucune approbation traçable n'a été fournie")
        if record.action != action:
            raise PermissionError("L'approbation ne couvre pas l'action demandée")
        if record.status != ApprovalStatus.APPROVED:
            raise PermissionError(f"Action non approuvée: statut {record.status.value}")
        return record
