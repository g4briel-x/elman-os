import sqlite3
import tempfile
import unittest
from pathlib import Path

from elman_os.domain import CycleResult, Verdict
from elman_os.metacognition import SupervisorPolicy
from elman_os.persistence import SQLiteKernelStore
from elman_os.studio_history import WorkflowHistoryReader
from elman_os.workflow import ElmanWorkflow


class StudioHistoryTests(unittest.TestCase):
    def test_missing_database_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing" / "elman.db"
            reader = WorkflowHistoryReader(database)

            self.assertFalse(reader.available)
            self.assertEqual(reader.list_runs(), ())
            self.assertIsNone(reader.get_run("unknown"))
            self.assertFalse(database.exists())
            self.assertFalse(database.parent.exists())

    def test_database_without_workflow_table_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "empty.db"
            sqlite3.connect(database).close()

            reader = WorkflowHistoryReader(database)

            self.assertTrue(reader.available)
            self.assertEqual(reader.list_runs(), ())
            self.assertIsNone(reader.get_run("unknown"))

    def _create_persisted_run(self, database: Path) -> None:
        store = SQLiteKernelStore(database)
        workflow = ElmanWorkflow(
            policy=SupervisorPolicy(
                max_iterations=3,
                max_same_failure=4,
                max_no_progress=4,
            ),
            report_sink=store.save_workflow,
        )

        def cycle(iteration: int, context: dict[str, object]) -> CycleResult:
            if iteration >= 2:
                return CycleResult(
                    proof_verdict=Verdict.PASS,
                    criteria_validated=True,
                    progress_score=1.0,
                    cost_units=1.0,
                    evidence=["PROOF-002"],
                )
            return CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.5,
                cost_units=1.0,
                evidence=["PROOF-001"],
                failure_fingerprint="studio-history-demo",
            )

        workflow.run(cycle, workflow_id="studio-history-run")

    def test_list_runs_exposes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            self._create_persisted_run(database)

            snapshots = WorkflowHistoryReader(database).list_runs()

            self.assertEqual(len(snapshots), 1)
            snapshot = snapshots[0]
            self.assertEqual(snapshot.workflow_id, "studio-history-run")
            self.assertEqual(snapshot.iteration_count, 2)
            self.assertEqual(snapshot.final_verdict, "PASS")
            self.assertEqual(snapshot.status, "ready_for_human_approval")

    def test_get_run_exposes_evidence_and_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            self._create_persisted_run(database)

            details = WorkflowHistoryReader(database).get_run(
                "studio-history-run"
            )

            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details.evidence, ("PROOF-001", "PROOF-002"))
            self.assertEqual(len(details.decisions), 2)
            self.assertIn("criteria_validated", details.decisions[-1])

    def test_reads_do_not_modify_database_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "elman.db"
            self._create_persisted_run(database)
            before = database.read_bytes()

            reader = WorkflowHistoryReader(database)
            reader.list_runs()
            reader.get_run("studio-history-run")

            self.assertEqual(database.read_bytes(), before)

    def test_limit_is_validated(self) -> None:
        reader = WorkflowHistoryReader("missing.db")
        with self.assertRaises(ValueError):
            reader.list_runs(0)


if __name__ == "__main__":
    unittest.main()
