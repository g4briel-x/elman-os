from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elman_os.final_verification import (
    FinalExecutionErrorRecord,
    FinalPolicyFinding,
    FinalReportSigner,
    FinalVerificationGate,
    FinalVerifier,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveDecisionAction,
)
from elman_os.studio_v07 import (
    StudioApprovalState,
    StudioDashboardSnapshot,
    StudioFinalState,
    StudioGateCard,
    StudioV07Error,
    StudioV07IntegrityError,
    StudioV07Projector,
    load_dashboard_snapshot,
    main,
)

# The final-verification suite owns the canonical offline fixture.  Reusing it
# here keeps Studio tests bound to the same real v0.7 contracts instead of
# introducing a weaker mock protocol.
try:  # unittest discovery adds tests/ directly to sys.path.
    from test_final_verification import (  # type: ignore[import-not-found]
        STEP_ID,
        T6,
        make_external_evidence,
        make_request,
        signer,
    )
except ModuleNotFoundError:  # direct ``python -m unittest tests...`` route
    from tests.test_final_verification import (
        STEP_ID,
        T6,
        make_external_evidence,
        make_request,
        signer,
    )


def project_verified():
    request = make_request()
    report_signer = signer()
    report = FinalVerifier(request, report_signer).verify()
    snapshot = StudioV07Projector(
        request,
        report,
        report_signer,
    ).project()
    return request, report_signer, report, snapshot


class VerifiedProjectionTests(unittest.TestCase):
    def test_verified_report_authorizes_completion(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(snapshot.final_state, StudioFinalState.VERIFIED)
        self.assertTrue(snapshot.signature_verified)
        self.assertTrue(snapshot.completion_authorized)

    def test_plan_and_step_are_visible(self):
        request, _, _, snapshot = project_verified()
        self.assertEqual(snapshot.plan_id, request.plan.plan_id)
        self.assertEqual(snapshot.project_id, request.plan.project_id)
        self.assertEqual(len(snapshot.steps), 1)
        self.assertEqual(snapshot.steps[0].step_id, STEP_ID)
        self.assertEqual(snapshot.steps[0].status, "completed")
        self.assertEqual(snapshot.steps[0].progress, 1.0)

    def test_selected_agents_and_capabilities_are_grouped(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(len(snapshot.agents), 1)
        agent = snapshot.agents[0]
        self.assertEqual(agent.agent_id, "ELMAN_BUILDER")
        self.assertEqual(agent.step_ids, (STEP_ID,))
        self.assertEqual(agent.capability_ids, ("build.release",))
        self.assertEqual(agent.failed_step_count, 0)

    def test_explicit_plan_approval_is_visible(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(len(snapshot.approvals), 1)
        approval = snapshot.approvals[0]
        self.assertEqual(approval.scope, "plan")
        self.assertEqual(approval.state, StudioApprovalState.GRANTED)
        self.assertEqual(approval.reference, "approval:final-release-001")

    def test_project_memory_decision_and_link_are_visible(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(len(snapshot.memory), 1)
        memory = snapshot.memory[0]
        self.assertEqual(memory.kind, "decision")
        self.assertEqual(memory.decision_link_state, "coherent")
        self.assertTrue(memory.payload_available)
        self.assertEqual(memory.revision_count, 1)

    def test_automatic_output_and_payload_evidence_are_visible(self):
        _, _, _, snapshot = project_verified()
        kinds = {item.kind for item in snapshot.evidence}
        self.assertEqual(kinds, {"output-validation", "artifact-integrity"})
        self.assertTrue(all(item.status == "verified" for item in snapshot.evidence))

    def test_metacognitive_clearance_is_visible(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(len(snapshot.supervision), 1)
        decision = snapshot.supervision[0]
        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.highest_risk, "info")
        self.assertEqual(decision.finding_count, 0)

    def test_all_nine_final_gates_are_visible(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(len(snapshot.gates), 9)
        self.assertEqual(
            {item.gate_id for item in snapshot.gates},
            {item.value for item in FinalVerificationGate},
        )
        self.assertTrue(all(item.passed for item in snapshot.gates))

    def test_projection_is_deterministic(self):
        request, report_signer, report, first = project_verified()
        second = StudioV07Projector(
            request,
            report,
            report_signer,
        ).project()
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)

    def test_snapshot_round_trip_preserves_hash_and_authorization(self):
        _, _, _, snapshot = project_verified()
        restored = StudioDashboardSnapshot.from_json(snapshot.to_json())
        self.assertEqual(restored, snapshot)
        restored.verify_hash()
        self.assertTrue(restored.completion_authorized)

    def test_snapshot_is_immutable(self):
        _, _, _, snapshot = project_verified()
        with self.assertRaises(FrozenInstanceError):
            snapshot.plan_status = "failed"  # type: ignore[misc]

    def test_serialization_never_contains_signing_secret(self):
        _, report_signer, _, snapshot = project_verified()
        serialized = snapshot.to_json()
        self.assertNotIn(report_signer.secret.hex(), serialized)
        self.assertNotIn("kkkkkkkk", serialized)
        self.assertIn(report_signer.key_id, serialized)

    def test_snapshot_progress_is_derived_from_step_state(self):
        _, _, _, snapshot = project_verified()
        self.assertEqual(snapshot.progress, 1.0)
        self.assertEqual(snapshot.to_dict()["progress"], 1.0)

    def test_journal_summary_is_bound_to_request(self):
        request, _, _, snapshot = project_verified()
        self.assertEqual(snapshot.journal_hash, request.journal_hash)
        self.assertEqual(
            snapshot.journal_event_count,
            request.journal_event_count,
        )


class ReportTrustTests(unittest.TestCase):
    def test_missing_report_remains_not_run(self):
        request = make_request()
        snapshot = StudioV07Projector(request).project()
        self.assertEqual(snapshot.final_state, StudioFinalState.NOT_RUN)
        self.assertFalse(snapshot.signature_verified)
        self.assertFalse(snapshot.completion_authorized)
        self.assertEqual(snapshot.gates, ())
        self.assertIsNone(snapshot.report_hash)

    def test_report_without_signer_fails_closed(self):
        request = make_request()
        report = FinalVerifier(request, signer()).verify()
        snapshot = StudioV07Projector(request, report).project()
        self.assertEqual(
            snapshot.final_state,
            StudioFinalState.SIGNATURE_UNVERIFIED,
        )
        self.assertFalse(snapshot.signature_verified)
        self.assertFalse(snapshot.completion_authorized)
        self.assertEqual(len(snapshot.gates), 9)

    def test_wrong_signing_key_is_rejected(self):
        request = make_request()
        report = FinalVerifier(request, signer()).verify()
        with self.assertRaisesRegex(
            StudioV07IntegrityError,
            "signature verification failed",
        ):
            StudioV07Projector(request, report, signer(byte=b"x")).project()

    def test_report_for_another_request_is_rejected(self):
        request = make_request()
        other = make_request(evidence=(make_external_evidence(),))
        other_signer = signer()
        other_report = FinalVerifier(other, other_signer).verify()
        with self.assertRaisesRegex(
            StudioV07IntegrityError,
            "not bound to the displayed request",
        ):
            StudioV07Projector(request, other_report, other_signer).project()

    def test_signed_rejected_report_is_exposed_as_rejected(self):
        request = make_request(include_output=False, include_payload=False)
        report_signer = signer()
        report = FinalVerifier(request, report_signer).verify()
        snapshot = StudioV07Projector(
            request,
            report,
            report_signer,
        ).project()
        self.assertEqual(snapshot.final_state, StudioFinalState.REJECTED)
        self.assertTrue(snapshot.signature_verified)
        self.assertFalse(snapshot.completion_authorized)
        self.assertTrue(any(not item.passed for item in snapshot.gates))

    def test_signer_without_report_is_rejected(self):
        with self.assertRaisesRegex(StudioV07Error, "without a report"):
            StudioV07Projector(make_request(), signer=signer())

    def test_invalid_request_type_is_rejected(self):
        with self.assertRaisesRegex(StudioV07Error, "FinalVerificationRequest"):
            StudioV07Projector(object())  # type: ignore[arg-type]

    def test_invalid_report_type_is_rejected(self):
        with self.assertRaisesRegex(StudioV07Error, "FinalVerificationReport"):
            StudioV07Projector(
                make_request(),
                object(),  # type: ignore[arg-type]
            )

    def test_invalid_signer_type_is_rejected(self):
        request = make_request()
        report = FinalVerifier(request, signer()).verify()
        with self.assertRaisesRegex(StudioV07Error, "FinalReportSigner"):
            StudioV07Projector(
                request,
                report,
                object(),  # type: ignore[arg-type]
            )


class IssueAndApprovalProjectionTests(unittest.TestCase):
    def test_unresolved_policy_finding_is_visible(self):
        finding = FinalPolicyFinding(
            finding_id="finding:studio-policy-001",
            rule_id="release.policy",
            summary="Release policy is not yet satisfied.",
            resolved=False,
            detected_at=T6,
        )
        snapshot = StudioV07Projector(
            make_request(policy_findings=(finding,))
        ).project()
        issue = next(item for item in snapshot.issues if item.source == "policy")
        self.assertEqual(issue.code, "release.policy")
        self.assertFalse(issue.resolved)
        self.assertIsNone(issue.evidence_reference)

    def test_resolved_policy_finding_keeps_evidence_reference(self):
        evidence = make_external_evidence(
            evidence_id="evidence:external:policy-resolution-001"
        )
        finding = FinalPolicyFinding(
            finding_id="finding:studio-policy-002",
            rule_id="release.policy",
            summary="Release policy was remediated.",
            resolved=True,
            resolution_evidence_id=evidence.evidence_id,
            detected_at=T6,
        )
        snapshot = StudioV07Projector(
            make_request(evidence=(evidence,), policy_findings=(finding,))
        ).project()
        issue = next(item for item in snapshot.issues if item.source == "policy")
        self.assertTrue(issue.resolved)
        self.assertEqual(issue.evidence_reference, evidence.evidence_id)

    def test_unresolved_execution_error_is_visible(self):
        error = FinalExecutionErrorRecord(
            error_id="error:studio-build-001",
            code="build.failure",
            summary="Build command failed.",
            resolved=False,
            step_id=STEP_ID,
            detected_at=T6,
        )
        snapshot = StudioV07Projector(
            make_request(execution_errors=(error,))
        ).project()
        issue = next(item for item in snapshot.issues if item.source == "execution")
        self.assertEqual(issue.step_id, STEP_ID)
        self.assertFalse(issue.resolved)

    def test_resolved_execution_error_keeps_resolution_evidence(self):
        evidence = make_external_evidence(
            evidence_id="evidence:external:error-resolution-001"
        )
        error = FinalExecutionErrorRecord(
            error_id="error:studio-build-002",
            code="build.failure",
            summary="Build command failure was corrected.",
            resolved=True,
            step_id=STEP_ID,
            resolution_evidence_id=evidence.evidence_id,
            detected_at=T6,
        )
        snapshot = StudioV07Projector(
            make_request(evidence=(evidence,), execution_errors=(error,))
        ).project()
        issue = next(item for item in snapshot.issues if item.source == "execution")
        self.assertTrue(issue.resolved)
        self.assertEqual(issue.evidence_reference, evidence.evidence_id)

    def test_paused_supervision_exposes_finding_and_approval(self):
        request = make_request(
            supervision_action=MetacognitiveDecisionAction.PAUSE
        )
        snapshot = StudioV07Projector(request).project()
        supervision = snapshot.supervision[0]
        self.assertEqual(supervision.action, "pause")
        self.assertEqual(supervision.highest_risk, "medium")
        self.assertEqual(supervision.finding_count, 1)
        self.assertTrue(
            any(item.source == "supervision" for item in snapshot.issues)
        )
        approval = next(
            item for item in snapshot.approvals if item.scope == "supervision"
        )
        self.assertEqual(approval.state, StudioApprovalState.GRANTED)
        self.assertIsNotNone(approval.reference)

    def test_missing_memory_remains_explicitly_empty(self):
        request = make_request(include_memory=False, include_link=False)
        snapshot = StudioV07Projector(request).project()
        self.assertEqual(snapshot.memory, ())


class SnapshotIntegrityTests(unittest.TestCase):
    def test_tampered_objective_is_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        data["objective"] = "Tampered objective"
        with self.assertRaisesRegex(
            StudioV07IntegrityError,
            "snapshot_hash",
        ):
            StudioDashboardSnapshot.from_dict(data)

    def test_invalid_snapshot_hash_is_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        data["snapshot_hash"] = "0" * 64
        with self.assertRaises(StudioV07IntegrityError):
            StudioDashboardSnapshot.from_dict(data)

    def test_missing_record_type_is_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        del data["record_type"]
        with self.assertRaisesRegex(StudioV07Error, "record_type"):
            StudioDashboardSnapshot.from_dict(data)

    def test_non_object_snapshot_json_is_rejected(self):
        with self.assertRaisesRegex(StudioV07Error, "must contain an object"):
            StudioDashboardSnapshot.from_json("[]")

    def test_invalid_snapshot_json_is_rejected(self):
        with self.assertRaisesRegex(StudioV07Error, "JSON is invalid"):
            StudioDashboardSnapshot.from_json("{")

    def test_tampered_completion_authorization_is_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        data["completion_authorized"] = False
        with self.assertRaisesRegex(
            StudioV07IntegrityError,
            "completion_authorized",
        ):
            StudioDashboardSnapshot.from_dict(data)

    def test_tampered_progress_is_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        data["progress"] = 0.25
        with self.assertRaisesRegex(StudioV07IntegrityError, "progress"):
            StudioDashboardSnapshot.from_dict(data)

    def test_duplicate_step_cards_are_rejected(self):
        _, _, _, snapshot = project_verified()
        data = snapshot.to_dict()
        data["steps"].append(dict(data["steps"][0]))
        with self.assertRaisesRegex(
            StudioV07IntegrityError,
            "duplicate identifiers",
        ):
            StudioDashboardSnapshot.from_dict(data)

    def test_gate_cannot_pass_with_issue_codes(self):
        with self.assertRaises(StudioV07IntegrityError):
            StudioGateCard(
                gate_id="plan-completion",
                passed=True,
                checked_count=1,
                issue_codes=("plan.incomplete",),
                references=(),
            )

    def test_gate_cannot_fail_without_issue_codes(self):
        with self.assertRaises(StudioV07IntegrityError):
            StudioGateCard(
                gate_id="plan-completion",
                passed=False,
                checked_count=1,
                issue_codes=(),
                references=(),
            )


class FileLoadingAndCliTests(unittest.TestCase):
    def test_load_request_without_report(self):
        request = make_request()
        with TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(request.to_json(), encoding="utf-8")
            snapshot = load_dashboard_snapshot(request_path)
        self.assertEqual(snapshot.final_state, StudioFinalState.NOT_RUN)

    def test_load_request_report_and_signer(self):
        request = make_request()
        report_signer = signer()
        report = FinalVerifier(request, report_signer).verify()
        with TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            report_path = Path(directory) / "report.json"
            request_path.write_text(request.to_json(), encoding="utf-8")
            report_path.write_text(report.to_json(), encoding="utf-8")
            snapshot = load_dashboard_snapshot(
                request_path,
                report_path=report_path,
                signer=report_signer,
            )
        self.assertTrue(snapshot.completion_authorized)

    def test_missing_request_file_is_rejected_without_side_effect(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(StudioV07Error, "does not exist"):
                load_dashboard_snapshot(missing)
            self.assertFalse(missing.exists())

    def test_invalid_request_file_is_rejected(self):
        with TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                StudioV07IntegrityError,
                "request file is invalid",
            ):
                load_dashboard_snapshot(request_path)

    def test_key_file_and_key_id_must_be_paired(self):
        with self.assertRaisesRegex(StudioV07Error, "supplied together"):
            main(
                [
                    "--request",
                    "request.json",
                    "--key-file",
                    "key.bin",
                ]
            )

    def test_key_file_requires_report(self):
        with self.assertRaisesRegex(StudioV07Error, "requires --report"):
            main(
                [
                    "--request",
                    "request.json",
                    "--key-file",
                    "key.bin",
                    "--key-id",
                    "key:studio-001",
                ]
            )

    def test_short_signing_key_is_rejected_by_final_contract(self):
        with self.assertRaises(ValueError):
            FinalReportSigner(key_id="key:studio-001", secret=b"short")


if __name__ == "__main__":
    unittest.main()
