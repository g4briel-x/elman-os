import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

from elman_os.domain import CycleResult, StopReason, Verdict
from elman_os.metacognition import SupervisorPolicy
from elman_os.persistence import SQLiteKernelStore
from elman_os.workflow import ElmanWorkflow


class PersistenceTests(unittest.TestCase):
    def test_every_connection_is_explicitly_closed(self) -> None:
        real_connect = sqlite3.connect
        connections: list[sqlite3.Connection] = []

        def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "elman_os.persistence.sqlite3.connect",
                side_effect=tracked_connect,
            ):
                store = SQLiteKernelStore(Path(directory) / "elman.db")
                self.assertEqual(store.list_workflows(), [])

            self.assertGreaterEqual(len(connections), 2)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_workflow_report_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteKernelStore(Path(directory) / "elman.db")
            workflow = ElmanWorkflow(
                policy=SupervisorPolicy(max_iterations=1),
                report_sink=store.save_workflow,
            )

            report = workflow.run(
                lambda iteration, context: CycleResult(
                    proof_verdict=Verdict.PASS,
                    criteria_validated=True,
                    progress_score=1.0,
                    cost_units=1.0,
                    evidence=["PROOF-001"],
                ),
                workflow_id="persisted-run",
            )

            stored = store.get_workflow("persisted-run")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["stop_reason"], StopReason.CRITERIA_VALIDATED)
            self.assertEqual(store.list_workflows()[0]["workflow_id"], "persisted-run")

    def test_limit_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteKernelStore(Path(directory) / "elman.db")
            with self.assertRaises(ValueError):
                store.list_workflows(0)


if __name__ == "__main__":
    unittest.main()
