import tempfile
import unittest
from pathlib import Path

from elman_os.approvals import ApprovalStatus, HumanApprovalGate
from elman_os.persistence import SQLiteKernelStore


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "elman.db"
        self.gate = HumanApprovalGate(SQLiteKernelStore(database))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_protected_action_requires_independent_approval(self) -> None:
        request = self.gate.request(
            action="production_deploy",
            requested_by="ELMAN_FORGE",
            reason="Livrer la version vérifiée",
        )
        with self.assertRaises(PermissionError):
            self.gate.require("production_deploy", request.request_id)

        approved = self.gate.decide(
            request.request_id,
            approved=True,
            decided_by="human-owner",
        )
        self.assertEqual(approved.status, ApprovalStatus.APPROVED)
        self.assertEqual(
            self.gate.require("production_deploy", request.request_id),
            approved,
        )

    def test_requester_cannot_self_approve(self) -> None:
        request = self.gate.request(
            action="activate_learning_proposal",
            requested_by="ELMAN_LEARNING",
            reason="Activer une proposition testée",
        )
        with self.assertRaises(PermissionError):
            self.gate.decide(
                request.request_id,
                approved=True,
                decided_by="ELMAN_LEARNING",
            )

    def test_approval_cannot_cover_another_action(self) -> None:
        request = self.gate.request(
            action="send_external_message",
            requested_by="ELMAN_CONNECT",
            reason="Notifier le propriétaire",
        )
        self.gate.decide(
            request.request_id,
            approved=True,
            decided_by="human-owner",
        )
        with self.assertRaises(PermissionError):
            self.gate.require("production_deploy", request.request_id)

    def test_approval_context_redacts_secrets(self) -> None:
        request = self.gate.request(
            action="use_real_customer_data",
            requested_by="ELMAN_DATA",
            reason="Exécuter un test approuvé",
            context={"api_key": "must-not-survive", "dataset": "pilot"},
        )
        self.assertEqual(request.context["api_key"], "[REDACTED]")
        self.assertEqual(request.context["dataset"], "pilot")


if __name__ == "__main__":
    unittest.main()
