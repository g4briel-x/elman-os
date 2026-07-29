import unittest

from elman_os.domain import CycleResult, StopReason, Verdict, WorkflowStatus
from elman_os.metacognition import SupervisorPolicy
from elman_os.workflow import ElmanWorkflow


class WorkflowTests(unittest.TestCase):
    def test_rework_then_pass(self) -> None:
        workflow = ElmanWorkflow(
            policy=SupervisorPolicy(
                max_iterations=4,
                max_same_failure=4,
                max_no_progress=4,
            )
        )

        def runner(iteration: int, context: dict[str, object]) -> CycleResult:
            if iteration == 2:
                return CycleResult(
                    proof_verdict=Verdict.PASS,
                    criteria_validated=True,
                    progress_score=1.0,
                    cost_units=1.0,
                    evidence=["AC-1"],
                )
            return CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.4,
                cost_units=1.0,
                evidence=["finding attribué"],
                failure_fingerprint="first-only",
            )

        report = workflow.run(runner, workflow_id="test-workflow")
        self.assertEqual(report.status, WorkflowStatus.READY_FOR_HUMAN_APPROVAL)
        self.assertEqual(report.stop_reason, StopReason.CRITERIA_VALIDATED)
        self.assertEqual(len(report.iterations), 2)
        self.assertEqual(len(report.learning_proposals), 1)

    def test_workflow_stops_at_limit(self) -> None:
        workflow = ElmanWorkflow(
            policy=SupervisorPolicy(
                max_iterations=3,
                max_same_failure=10,
                max_no_progress=10,
            )
        )

        def runner(iteration: int, context: dict[str, object]) -> CycleResult:
            return CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=iteration / 10,
                cost_units=1.0,
                evidence=["cycle"],
                failure_fingerprint=f"failure-{iteration}",
            )

        report = workflow.run(runner)
        self.assertEqual(report.stop_reason, StopReason.MAX_ITERATIONS)
        self.assertEqual(report.status, WorkflowStatus.STOPPED_LIMIT)
        self.assertEqual(len(report.iterations), 3)


if __name__ == "__main__":
    unittest.main()
