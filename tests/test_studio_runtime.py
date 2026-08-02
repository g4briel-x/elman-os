import tempfile
import unittest
from pathlib import Path

from elman_os.studio_history import WorkflowHistoryReader
from elman_os.studio_runtime import (
    LocalWorkflowEvent,
    LocalWorkflowRequest,
    LocalWorkflowRunner,
)


class StudioRuntimeTests(unittest.TestCase):
    def test_request_validation_is_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            LocalWorkflowRequest(
                workflow_id="../unsafe",
                pass_on=1,
                max_iterations=2,
            )
        with self.assertRaises(ValueError):
            LocalWorkflowRequest(
                workflow_id="valid-run",
                pass_on=0,
                max_iterations=2,
            )

    def test_explicit_approval_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            runner = LocalWorkflowRunner(database)

            with self.assertRaises(PermissionError):
                runner.run(
                    LocalWorkflowRequest(
                        workflow_id="approval-required",
                        pass_on=1,
                        max_iterations=2,
                    ),
                    approved=False,
                )

            self.assertFalse(database.exists())

    def test_workflow_emits_progress_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            events: list[LocalWorkflowEvent] = []
            runner = LocalWorkflowRunner(database)

            report = runner.run(
                LocalWorkflowRequest(
                    workflow_id="studio-live-run",
                    pass_on=2,
                    max_iterations=4,
                ),
                approved=True,
                on_event=events.append,
            )

            self.assertEqual(report.workflow_id, "studio-live-run")
            self.assertEqual(len(report.iterations), 2)
            self.assertEqual(
                [event.kind for event in events],
                ["started", "iteration", "iteration", "completed"],
            )
            self.assertEqual(events[-1].verdict, "PASS")
            self.assertEqual(events[-1].stop_reason, "criteria_validated")

            history = WorkflowHistoryReader(database)
            snapshots = history.list_runs()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].workflow_id, "studio-live-run")
            self.assertEqual(snapshots[0].iteration_count, 2)

    def test_iteration_limit_stops_without_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            report = LocalWorkflowRunner(database).run(
                LocalWorkflowRequest(
                    workflow_id="studio-limited-run",
                    pass_on=3,
                    max_iterations=2,
                ),
                approved=True,
            )

            self.assertEqual(len(report.iterations), 2)
            self.assertEqual(report.stop_reason.value, "max_iterations")
            self.assertEqual(report.status.value, "stopped_limit")
            self.assertEqual(
                report.iterations[-1].result.proof_verdict.value,
                "REWORK_REQUIRED",
            )


if __name__ == "__main__":
    unittest.main()
