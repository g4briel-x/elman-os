import unittest

from elman_os.domain import CycleResult, StopReason, Verdict
from elman_os.metacognition import (
    LearningAgent,
    MemoryManager,
    MetacognitiveSupervisor,
    ReflectiveAgent,
    SupervisorPolicy,
)


class SupervisorTests(unittest.TestCase):
    def test_success_stops_for_human_approval(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.PASS,
                criteria_validated=True,
                progress_score=1.0,
                cost_units=1.0,
                evidence=["test passé"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertFalse(decision.should_continue)
        self.assertEqual(decision.reason, StopReason.CRITERIA_VALIDATED)
        self.assertTrue(decision.requires_human_decision)

    def test_pass_with_warnings_stops_for_human_approval(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.PASS_WITH_WARNINGS,
                criteria_validated=True,
                progress_score=1.0,
                cost_units=1.0,
                evidence=["test passé", "risque mineur documenté"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.CRITERIA_VALIDATED)
        self.assertIn("avertissements", decision.message)

    def test_repeated_failure_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(max_iterations=5, max_same_failure=2)
        )
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.2,
            cost_units=1.0,
            failure_fingerprint="same",
        )
        first = supervisor.evaluate(
            iteration=1,
            result=result,
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        second = supervisor.evaluate(
            iteration=2,
            result=result,
            cumulative_cost=2.0,
            elapsed_seconds=2.0,
        )
        self.assertTrue(first.should_continue)
        self.assertEqual(second.reason, StopReason.REPEATED_FAILURE)

    def test_critical_finding_stops_immediately(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.BLOCKED,
                criteria_validated=False,
                progress_score=0.1,
                cost_units=1.0,
                critical_findings=["secret exposé"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.CRITICAL_FINDING)

    def test_max_iterations_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(
                max_iterations=2,
                max_same_failure=10,
                max_no_progress=10,
            )
        )
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.2,
            cost_units=1.0,
        )
        supervisor.evaluate(
            iteration=1,
            result=result,
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        decision = supervisor.evaluate(
            iteration=2,
            result=result,
            cumulative_cost=2.0,
            elapsed_seconds=2.0,
        )
        self.assertEqual(decision.reason, StopReason.MAX_ITERATIONS)

    def test_no_progress_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(
                max_iterations=6,
                max_same_failure=10,
                max_no_progress=2,
                minimum_progress_delta=0.05,
            )
        )
        for iteration in (1, 2):
            decision = supervisor.evaluate(
                iteration=iteration,
                result=CycleResult(
                    proof_verdict=Verdict.REWORK_REQUIRED,
                    criteria_validated=False,
                    progress_score=0.5,
                    cost_units=1.0,
                    failure_fingerprint=f"failure-{iteration}",
                ),
                cumulative_cost=float(iteration),
                elapsed_seconds=float(iteration),
            )
        self.assertTrue(decision.should_continue)
        decision = supervisor.evaluate(
            iteration=3,
            result=CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.5,
                cost_units=1.0,
                failure_fingerprint="failure-3",
            ),
            cumulative_cost=3.0,
            elapsed_seconds=3.0,
        )
        self.assertEqual(decision.reason, StopReason.NO_PROGRESS)

    def test_budget_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(max_cost_units=2.0)
        )
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.2,
                cost_units=2.0,
            ),
            cumulative_cost=2.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.BUDGET_EXHAUSTED)

    def test_external_blocker_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.BLOCKED,
                criteria_validated=False,
                progress_score=0.0,
                cost_units=0.0,
                blocked_reason="Validation du propriétaire requise",
            ),
            cumulative_cost=0.0,
            elapsed_seconds=0.1,
        )
        self.assertEqual(decision.reason, StopReason.EXTERNAL_BLOCKER)
        self.assertTrue(decision.requires_human_decision)


class MemoryAndLearningTests(unittest.TestCase):
    def test_memory_redacts_secrets(self) -> None:
        memory = MemoryManager()
        memory.remember_working("api_key", "should-not-survive")
        memory.record_episode({"password": "also-secret", "result": "ok"})
        snapshot = memory.snapshot()
        self.assertEqual(snapshot["working"]["api_key"], "[REDACTED]")
        self.assertEqual(snapshot["episodes"][0]["password"], "[REDACTED]")

    def test_learning_requires_explicit_approval(self) -> None:
        learner = LearningAgent()
        reflector = ReflectiveAgent()
        result = CycleResult(
            proof_verdict=Verdict.PASS,
            criteria_validated=True,
            progress_score=1.0,
            cost_units=1.0,
            evidence=["preuve"],
        )
        reflection = reflector.review(1, result, None)
        proposal = learner.propose("wf", result, reflection)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertFalse(proposal.approved)

        memory = MemoryManager()
        memory.approve_lesson(proposal, approved_by="human-reviewer")
        self.assertEqual(len(memory.semantic), 1)


if __name__ == "__main__":
    unittest.main()
