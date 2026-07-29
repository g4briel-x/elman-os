"""Bounded metacognitive supervision for ELMAN-OS."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .domain import (
    CycleResult,
    LearningProposal,
    ReflectionReport,
    StopReason,
    SupervisorDecision,
    Verdict,
)


_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|password|passwd|secret|token|private[_-]?key|credential)",
    re.IGNORECASE,
)


def redact_sensitive(value: Any, key: str = "") -> Any:
    """Remove obvious secrets before data enters metacognitive memory."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value


@dataclass(slots=True)
class SupervisorPolicy:
    max_iterations: int = 5
    max_same_failure: int = 2
    max_no_progress: int = 2
    minimum_progress_delta: float = 0.01
    max_cost_units: float = 100.0
    max_elapsed_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations doit être supérieur ou égal à 1")
        if self.max_same_failure < 1:
            raise ValueError("max_same_failure doit être supérieur ou égal à 1")
        if self.max_no_progress < 1:
            raise ValueError("max_no_progress doit être supérieur ou égal à 1")
        if self.max_cost_units <= 0:
            raise ValueError("max_cost_units doit être positif")
        if self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds doit être positif")


@dataclass(slots=True)
class MemoryManager:
    """Three-level memory with explicit provenance and approval."""

    working: dict[str, Any] = field(default_factory=dict)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    semantic: dict[str, dict[str, Any]] = field(default_factory=dict)

    def remember_working(self, key: str, value: Any) -> None:
        self.working[key] = redact_sensitive(value, key)

    def record_episode(self, episode: dict[str, Any]) -> None:
        clean = redact_sensitive(episode)
        if not isinstance(clean, dict):
            raise TypeError("Un épisode doit être un dictionnaire")
        self.episodes.append(clean)

    def approve_lesson(self, proposal: LearningProposal, approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("Une leçon exige un approbateur identifiable")
        key = hashlib.sha256(proposal.pattern.encode("utf-8")).hexdigest()[:16]
        self.semantic[key] = {
            "pattern": proposal.pattern,
            "evidence": list(proposal.evidence),
            "confidence": proposal.confidence,
            "approved_by": approved_by,
        }

    def snapshot(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "working": self.working,
                "episodes": self.episodes,
                "semantic": self.semantic,
            }
        )


@dataclass(slots=True)
class ReflectiveAgent:
    """Produces a concise post-cycle reflection without changing production code."""

    def review(
        self,
        iteration: int,
        result: CycleResult,
        previous: CycleResult | None,
    ) -> ReflectionReport:
        worked: list[str] = []
        failed: list[str] = []
        gaps: list[str] = []

        if result.progress_score > 0:
            worked.append(f"Progression observée: {result.progress_score:.2f}")
        if result.evidence:
            worked.append(f"{len(result.evidence)} preuve(s) fournie(s)")
        else:
            gaps.append("Aucune preuve fournie par le cycle")

        if result.proof_verdict in {Verdict.REWORK_REQUIRED, Verdict.BLOCKED}:
            failed.append(f"Verdict final: {result.proof_verdict.value}")
        if result.critical_findings:
            failed.append("Anomalie critique détectée")
        if previous and result.progress_score <= previous.progress_score:
            failed.append("Absence d'amélioration mesurable depuis le cycle précédent")

        correction = None
        if result.blocked_reason:
            correction = "Escalader le blocage avec les preuves et la décision exacte attendue."
        elif result.critical_findings:
            correction = "Arrêter la production et soumettre le risque critique à décision humaine."
        elif result.proof_verdict == Verdict.REWORK_REQUIRED:
            correction = "Réattribuer chaque finding à son propriétaire puis retester uniquement les gates affectées."
        elif not result.criteria_validated:
            correction = "Cibler le critère d'acceptation non satisfait ayant le plus fort impact."

        return ReflectionReport(
            iteration=iteration,
            what_worked=tuple(worked),
            what_failed=tuple(failed),
            evidence_gaps=tuple(gaps),
            recommended_correction=correction,
            failure_fingerprint=result.failure_fingerprint,
        )


@dataclass(slots=True)
class LearningAgent:
    """Proposes lessons; it never activates them automatically."""

    def propose(
        self,
        workflow_id: str,
        result: CycleResult,
        reflection: ReflectionReport,
    ) -> LearningProposal | None:
        if result.proof_verdict != Verdict.PASS or not result.criteria_validated:
            return None
        if not result.evidence:
            return None
        raw = f"{workflow_id}:{reflection.iteration}:{'|'.join(result.evidence)}"
        proposal_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return LearningProposal(
            proposal_id=proposal_id,
            pattern=(
                "Réutiliser les contrôles qui ont permis de satisfaire les critères, "
                "après revue humaine de leur portée et de leur généralisation."
            ),
            evidence=tuple(result.evidence),
            confidence="medium",
            approved=False,
        )


@dataclass(slots=True)
class MetacognitiveSupervisor:
    policy: SupervisorPolicy
    _failure_counts: dict[str, int] = field(default_factory=dict)
    _no_progress_count: int = 0
    _previous_progress: float | None = None

    def evaluate(
        self,
        *,
        iteration: int,
        result: CycleResult,
        cumulative_cost: float,
        elapsed_seconds: float,
        cancelled: bool = False,
    ) -> SupervisorDecision:
        if cancelled:
            return SupervisorDecision(
                False,
                StopReason.CANCELLED,
                "Exécution annulée par l'utilisateur.",
                requires_human_decision=False,
            )

        if result.blocked_reason:
            return SupervisorDecision(
                False,
                StopReason.EXTERNAL_BLOCKER,
                result.blocked_reason,
                requires_human_decision=True,
            )

        if result.critical_findings:
            return SupervisorDecision(
                False,
                StopReason.CRITICAL_FINDING,
                "Un finding critique exige une décision humaine avant toute reprise.",
                requires_human_decision=True,
            )

        if result.proof_verdict in {
            Verdict.PASS,
            Verdict.PASS_WITH_WARNINGS,
        } and result.criteria_validated:
            warning = (
                " avec avertissements"
                if result.proof_verdict == Verdict.PASS_WITH_WARNINGS
                else ""
            )
            return SupervisorDecision(
                False,
                StopReason.CRITERIA_VALIDATED,
                f"Les critères sont validés{warning} par ELMAN Proof; "
                "approbation humaine de livraison requise.",
                requires_human_decision=True,
            )

        if cumulative_cost >= self.policy.max_cost_units:
            return SupervisorDecision(
                False,
                StopReason.BUDGET_EXHAUSTED,
                "Budget maximal de la boucle atteint.",
                requires_human_decision=True,
            )

        if elapsed_seconds >= self.policy.max_elapsed_seconds:
            return SupervisorDecision(
                False,
                StopReason.TIME_LIMIT,
                "Durée maximale de la boucle atteinte.",
                requires_human_decision=True,
            )

        if result.failure_fingerprint:
            count = self._failure_counts.get(result.failure_fingerprint, 0) + 1
            self._failure_counts[result.failure_fingerprint] = count
            if count >= self.policy.max_same_failure:
                return SupervisorDecision(
                    False,
                    StopReason.REPEATED_FAILURE,
                    "Le même échec se répète; une décision humaine est nécessaire.",
                    requires_human_decision=True,
                )

        if self._previous_progress is not None:
            delta = result.progress_score - self._previous_progress
            if delta < self.policy.minimum_progress_delta:
                self._no_progress_count += 1
            else:
                self._no_progress_count = 0
            if self._no_progress_count >= self.policy.max_no_progress:
                return SupervisorDecision(
                    False,
                    StopReason.NO_PROGRESS,
                    "La boucle n'améliore plus le résultat de manière mesurable.",
                    requires_human_decision=True,
                )
        self._previous_progress = result.progress_score

        if iteration >= self.policy.max_iterations:
            return SupervisorDecision(
                False,
                StopReason.MAX_ITERATIONS,
                "Nombre maximal d'itérations atteint.",
                requires_human_decision=True,
            )

        return SupervisorDecision(
            True,
            StopReason.CONTINUE,
            "Une correction ciblée est autorisée dans le même périmètre.",
            requires_human_decision=False,
        )


def memory_to_json(memory: MemoryManager) -> str:
    return json.dumps(memory.snapshot(), ensure_ascii=False, indent=2)
