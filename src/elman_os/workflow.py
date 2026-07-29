"""Deterministic workflow shell around the ELMAN-OS metacognitive loop."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable
from uuid import uuid4

from .domain import (
    CycleResult,
    IterationRecord,
    StopReason,
    WorkflowReport,
    WorkflowStatus,
)
from .metacognition import (
    LearningAgent,
    MemoryManager,
    MetacognitiveSupervisor,
    ReflectiveAgent,
    SupervisorPolicy,
)


CycleRunner = Callable[[int, dict[str, object]], CycleResult]


@dataclass(slots=True)
class ElmanWorkflow:
    policy: SupervisorPolicy = field(default_factory=SupervisorPolicy)
    memory: MemoryManager = field(default_factory=MemoryManager)
    reflector: ReflectiveAgent = field(default_factory=ReflectiveAgent)
    learner: LearningAgent = field(default_factory=LearningAgent)
    report_sink: Callable[[WorkflowReport], None] | None = None

    def run(
        self,
        cycle_runner: CycleRunner,
        *,
        workflow_id: str | None = None,
        initial_context: dict[str, object] | None = None,
    ) -> WorkflowReport:
        """Run a bounded loop.

        The cycle runner owns production work. This method owns supervision,
        reflection, memory, learning proposals and stop conditions.
        """
        workflow_id = workflow_id or f"elman-{uuid4().hex[:12]}"
        context = dict(initial_context or {})
        supervisor = MetacognitiveSupervisor(self.policy)
        iterations: list[IterationRecord] = []
        proposals = []
        cumulative_cost = 0.0
        previous = None
        started = time.monotonic()

        self.memory.remember_working("workflow_id", workflow_id)
        self.memory.remember_working("initial_context", context)

        status = WorkflowStatus.RUNNING
        stop_reason = StopReason.CONTINUE

        for iteration in range(1, self.policy.max_iterations + 1):
            result = cycle_runner(iteration, context)
            if not 0.0 <= result.progress_score <= 1.0:
                raise ValueError("progress_score doit être compris entre 0 et 1")
            if result.cost_units < 0:
                raise ValueError("cost_units ne peut pas être négatif")

            cumulative_cost += result.cost_units
            reflection = self.reflector.review(iteration, result, previous)
            elapsed = time.monotonic() - started
            decision = supervisor.evaluate(
                iteration=iteration,
                result=result,
                cumulative_cost=cumulative_cost,
                elapsed_seconds=elapsed,
            )
            record = IterationRecord(
                iteration=iteration,
                result=result,
                reflection=reflection,
                decision=decision,
            )
            iterations.append(record)
            self.memory.record_episode(
                {
                    "workflow_id": workflow_id,
                    "iteration": iteration,
                    "result": asdict(result),
                    "reflection": asdict(reflection),
                    "decision": asdict(decision),
                }
            )

            proposal = self.learner.propose(workflow_id, result, reflection)
            if proposal:
                proposals.append(proposal)

            context["last_reflection"] = asdict(reflection)
            context["last_decision"] = asdict(decision)
            previous = result

            if not decision.should_continue:
                stop_reason = decision.reason
                if decision.reason == StopReason.CRITERIA_VALIDATED:
                    status = WorkflowStatus.READY_FOR_HUMAN_APPROVAL
                elif decision.reason in {
                    StopReason.MAX_ITERATIONS,
                    StopReason.BUDGET_EXHAUSTED,
                    StopReason.TIME_LIMIT,
                    StopReason.CANCELLED,
                }:
                    status = WorkflowStatus.STOPPED_LIMIT
                elif decision.requires_human_decision:
                    status = WorkflowStatus.BLOCKED
                else:
                    status = WorkflowStatus.STOPPED_LIMIT
                break

        if stop_reason == StopReason.CONTINUE:
            # Defensive fallback. The supervisor should stop on max_iterations.
            stop_reason = StopReason.MAX_ITERATIONS
            status = WorkflowStatus.STOPPED_LIMIT

        self.memory.remember_working("cumulative_cost", cumulative_cost)
        self.memory.remember_working("final_status", status.value)
        self.memory.remember_working("stop_reason", stop_reason.value)

        report = WorkflowReport(
            workflow_id=workflow_id,
            status=status,
            stop_reason=stop_reason,
            iterations=iterations,
            learning_proposals=proposals,
            memory_snapshot=self.memory.snapshot(),
        )
        if self.report_sink is not None:
            self.report_sink(report)
        return report
