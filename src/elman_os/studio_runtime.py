"""Human-approved local workflow execution for ELMAN Studio."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .domain import CycleResult, Verdict, WorkflowReport
from .metacognition import SupervisorPolicy
from .persistence import SQLiteKernelStore
from .workflow import ElmanWorkflow


_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


@dataclass(frozen=True, slots=True)
class LocalWorkflowRequest:
    """Validated parameters for one bounded deterministic workflow."""

    workflow_id: str
    pass_on: int
    max_iterations: int

    def __post_init__(self) -> None:
        identifier = self.workflow_id.strip()
        object.__setattr__(self, "workflow_id", identifier)

        if not _WORKFLOW_ID.fullmatch(identifier):
            raise ValueError(
                "workflow_id doit contenir 3 à 64 caractères portables "
                "(lettres, chiffres, point, tiret ou underscore)"
            )
        if not 1 <= self.max_iterations <= 50:
            raise ValueError("max_iterations doit être compris entre 1 et 50")
        if not 1 <= self.pass_on <= 50:
            raise ValueError("pass_on doit être compris entre 1 et 50")


@dataclass(frozen=True, slots=True)
class LocalWorkflowEvent:
    """Progress event emitted without exposing prompts, secrets or payloads."""

    kind: str
    workflow_id: str
    message: str
    iteration: int = 0
    max_iterations: int = 0
    progress: float = 0.0
    verdict: str | None = None
    stop_reason: str | None = None


ProgressCallback = Callable[[LocalWorkflowEvent], None]


class LocalWorkflowRunner:
    """Run and persist a deterministic workflow after explicit approval."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        iteration_delay_seconds: float = 0.0,
    ) -> None:
        if iteration_delay_seconds < 0:
            raise ValueError("iteration_delay_seconds ne peut pas être négatif")
        self.database_path = Path(database_path).expanduser().resolve()
        self.iteration_delay_seconds = iteration_delay_seconds

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        event: LocalWorkflowEvent,
    ) -> None:
        if callback is not None:
            callback(event)

    def run(
        self,
        request: LocalWorkflowRequest,
        *,
        approved: bool,
        on_event: ProgressCallback | None = None,
    ) -> WorkflowReport:
        """Execute a bounded local workflow and persist its final report."""

        if not approved:
            raise PermissionError(
                "Une approbation humaine explicite est requise avant exécution"
            )

        self._emit(
            on_event,
            LocalWorkflowEvent(
                kind="started",
                workflow_id=request.workflow_id,
                message="Workflow local démarré",
                max_iterations=request.max_iterations,
            ),
        )

        store = SQLiteKernelStore(self.database_path)
        workflow = ElmanWorkflow(
            policy=SupervisorPolicy(
                max_iterations=request.max_iterations,
                max_same_failure=request.max_iterations + 1,
                max_no_progress=request.max_iterations + 1,
            ),
            report_sink=store.save_workflow,
        )

        def deterministic_cycle(
            iteration: int,
            context: dict[str, object],
        ) -> CycleResult:
            passed = iteration >= request.pass_on
            progress = (
                1.0
                if passed
                else min(0.95, iteration / max(request.pass_on, 1))
            )
            verdict = Verdict.PASS if passed else Verdict.REWORK_REQUIRED
            evidence = [
                (
                    f"STUDIO-LOCAL-{iteration:03d}: "
                    f"cycle déterministe {iteration} exécuté"
                )
            ]

            self._emit(
                on_event,
                LocalWorkflowEvent(
                    kind="iteration",
                    workflow_id=request.workflow_id,
                    message=(
                        f"Itération {iteration}/{request.max_iterations} : "
                        f"{verdict.value}"
                    ),
                    iteration=iteration,
                    max_iterations=request.max_iterations,
                    progress=progress,
                    verdict=verdict.value,
                ),
            )
            if self.iteration_delay_seconds:
                time.sleep(self.iteration_delay_seconds)

            return CycleResult(
                proof_verdict=verdict,
                criteria_validated=passed,
                progress_score=progress,
                cost_units=1.0,
                evidence=evidence,
                failure_fingerprint=(
                    None if passed else f"studio-local-{iteration}"
                ),
            )

        try:
            report = workflow.run(
                deterministic_cycle,
                workflow_id=request.workflow_id,
                initial_context={
                    "execution_mode": "studio_local_deterministic",
                    "remote_provider": False,
                    "deployment": False,
                },
            )
        except Exception as exc:
            self._emit(
                on_event,
                LocalWorkflowEvent(
                    kind="failed",
                    workflow_id=request.workflow_id,
                    message=f"Échec du workflow local : {exc}",
                    max_iterations=request.max_iterations,
                ),
            )
            raise

        final_verdict = (
            report.iterations[-1].result.proof_verdict.value
            if report.iterations
            else None
        )
        self._emit(
            on_event,
            LocalWorkflowEvent(
                kind="completed",
                workflow_id=request.workflow_id,
                message=(
                    f"Workflow terminé : {report.status.value} / "
                    f"{report.stop_reason.value}"
                ),
                iteration=len(report.iterations),
                max_iterations=request.max_iterations,
                progress=1.0,
                verdict=final_verdict,
                stop_reason=report.stop_reason.value,
            ),
        )
        return report
