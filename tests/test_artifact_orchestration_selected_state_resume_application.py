from dataclasses import FrozenInstanceError, replace
import json
import unittest

from elman_os.artifact_orchestration_selected_state_resume_application import (
    ArtifactOrchestrationSelectedStateResumeApplication,
    ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError,
    ArtifactOrchestrationSelectedStateResumeApplicationError,
    ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError,
    ArtifactOrchestrationSelectedStateResumeApplicationPolicy,
    ArtifactOrchestrationSelectedStateResumeApplicationRequest,
    ArtifactOrchestrationSelectedStateResumeApplicationResult,
    ArtifactOrchestrationSelectedStateResumeApplicationStatus,
)
from elman_os.execution_journal import ExecutionEventType
from elman_os.execution_plan import StepStatus
from elman_os.resume_application import ResumeApplicationStatus
from test_artifact_orchestration_selected_state_resume_authorization import (
    authorize,
    make_source_result,
)


REQUESTED_AT = "2026-08-06T01:02:00Z"


def make_policy(**changes):
    values = {
        "policy_id": "policy:selected-state-resume-application",
        "require_approved_authorization": True,
        "require_source_immutability": True,
        "allow_already_applied": True,
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateResumeApplicationPolicy(**values)


def make_request(authorization=None, policy=None, **changes):
    effective_authorization = authorization or authorize(
        make_source_result("ready")
    )
    effective_policy = policy or make_policy()
    values = {
        "authorization_result": effective_authorization,
        "policy": effective_policy,
        "requested_by": "ELMAN_NEXUS",
        "requested_at": REQUESTED_AT,
        "rationale": "Apply the explicitly authorized resume command in memory.",
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_authorization_result(
        **values
    )


def apply_authorization(authorization=None, policy=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(
        authorization=authorization,
        policy=effective_policy,
        **request_changes,
    )
    return ArtifactOrchestrationSelectedStateResumeApplication(
        request,
        effective_policy,
    ).apply()


class SelectedStateResumeApplicationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(allow_already_applied=False)
        self.assertEqual(
            ArtifactOrchestrationSelectedStateResumeApplicationPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_rejects_disabled_authorization_requirement(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            make_policy(require_approved_authorization=False)

    def test_policy_rejects_disabled_source_immutability(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            make_policy(require_source_immutability=False)

    def test_policy_requires_boolean_values(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            make_policy(allow_already_applied="yes")

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class SelectedStateResumeApplicationRequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        authorization = authorize(make_source_result("ready"))
        self.assertEqual(
            make_request(authorization=authorization).application_request_id,
            make_request(authorization=authorization).application_request_id,
        )

    def test_request_json_round_trip(self):
        request = make_request()
        restored = ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_json(
            request.to_json()
        )
        self.assertEqual(restored, request)
        restored.verify_hash()

    def test_request_rejects_time_before_authorization(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError
        ):
            make_request(requested_at="2026-08-06T01:00:00Z")

    def test_request_rejects_invalid_agent_identifier(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            make_request(requested_by="human:operator")

    def test_request_rejects_tampered_hash(self):
        request = make_request()
        data = request.to_dict()
        data["rationale"] = "Tampered rationale"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_dict(
                data
            )

    def test_request_rejects_tampered_authorization_hash(self):
        request = make_request()
        data = request.to_dict()
        data["authorization_result_hash"] = "0" * 64
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_dict(
                data
            )

    def test_request_rejects_missing_request_hash(self):
        request = make_request()
        data = request.to_dict()
        del data["request_hash"]
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_dict(
                data
            )

    def test_request_factory_rejects_wrong_authorization_type(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationRequest.from_authorization_result(
                authorization_result="invalid",  # type: ignore[arg-type]
                policy=make_policy(),
                requested_by="ELMAN_NEXUS",
                requested_at=REQUESTED_AT,
                rationale="Apply",
            )


class SelectedStateResumeApplicationTests(unittest.TestCase):
    def test_ready_authorization_is_applied(self):
        result = apply_authorization()
        self.assertIs(
            result.status,
            ArtifactOrchestrationSelectedStateResumeApplicationStatus.APPLIED,
        )
        self.assertIs(
            result.resume_application_result.status,
            ResumeApplicationStatus.APPLIED,
        )

    def test_application_approves_selected_step(self):
        result = apply_authorization()
        step = result.updated_plan.steps[0]
        self.assertIs(step.status, StepStatus.APPROVED)
        self.assertEqual(step.approval_reference, "approval:resume-001")

    def test_application_handles_two_selected_steps(self):
        authorization = authorize(make_source_result("ready", 2))
        result = apply_authorization(authorization)
        self.assertEqual(
            result.resume_application_result.selected_step_ids,
            ("step.1", "step.2"),
        )
        self.assertTrue(
            all(
                step.status is StepStatus.APPROVED
                for step in result.updated_plan.steps
            )
        )

    def test_application_appends_step_approval_events(self):
        result = apply_authorization()
        events = result.updated_journal.events
        self.assertIs(events[-1].event_type, ExecutionEventType.STEP_APPROVED)
        self.assertEqual(events[-1].step_id, "step.1")
        self.assertEqual(
            events[-1].payload["resume_approval_reference"],
            "approval:resume-001",
        )

    def test_application_result_binds_authorization_command(self):
        authorization = authorize(make_source_result("ready"))
        result = apply_authorization(authorization)
        command = authorization.command
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(
            result.resume_application_result.command_hash,
            command.command_hash,
        )
        self.assertEqual(
            result.resume_application_result.command_id,
            command.command_id,
        )

    def test_source_authorization_and_state_are_unchanged(self):
        authorization = authorize(make_source_result("ready", 2))
        restored = authorization.authorization_request.restoration_result.restored_state
        before = {
            "authorization": authorization.to_json(),
            "plan": restored.plan.to_json(),
            "journal": restored.journal.to_jsonl(),
            "checkpoint": restored.checkpoint.to_json(),
        }
        apply_authorization(authorization)
        after = {
            "authorization": authorization.to_json(),
            "plan": restored.plan.to_json(),
            "journal": restored.journal.to_jsonl(),
            "checkpoint": restored.checkpoint.to_json(),
        }
        self.assertEqual(after, before)

    def test_updated_plan_is_a_distinct_value(self):
        authorization = authorize(make_source_result("ready"))
        source_plan = (
            authorization.authorization_request.restoration_result.restored_state.plan
        )
        result = apply_authorization(authorization)
        self.assertIsNot(result.updated_plan, source_plan)
        self.assertNotEqual(result.updated_plan.to_json(), source_plan.to_json())

    def test_repeated_application_is_deterministic(self):
        authorization = authorize(make_source_result("ready", 2))
        first = apply_authorization(authorization)
        second = apply_authorization(authorization)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.result_hash, second.result_hash)

    def test_rejected_authorization_is_refused(self):
        authorization = authorize(make_source_result("blocked"))
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationAuthorizationError
        ):
            apply_authorization(authorization)

    def test_constructor_rejects_different_supplied_policy(self):
        request = make_request(policy=make_policy())
        other = make_policy(allow_already_applied=False)
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationError
        ):
            ArtifactOrchestrationSelectedStateResumeApplication(request, other)

    def test_result_json_round_trip(self):
        result = apply_authorization()
        restored = ArtifactOrchestrationSelectedStateResumeApplicationResult.from_json(
            result.to_json()
        )
        self.assertEqual(restored, result)
        restored.verify_hash()

    def test_result_rejects_tampered_hash(self):
        result = apply_authorization()
        data = result.to_dict()
        data["reason"] = "Tampered"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationResult.from_dict(
                data
            )

    def test_result_rejects_status_mismatch(self):
        result = apply_authorization()
        data = result.to_dict()
        data["status"] = "already-applied"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationResult.from_dict(
                data
            )

    def test_result_rejects_tampered_embedded_application(self):
        result = apply_authorization()
        data = result.to_dict()
        embedded = json.loads(data["resume_application_result_json"])
        embedded["command_hash"] = "0" * 64
        data["resume_application_result_json"] = json.dumps(embedded)
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeApplicationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeApplicationResult.from_dict(
                data
            )

    def test_result_updated_journal_validates(self):
        result = apply_authorization()
        result.updated_journal.validate()
        self.assertEqual(
            result.updated_journal.event_count,
            result.resume_application_result.journal_after_event_count,
        )

    def test_result_is_frozen(self):
        result = apply_authorization()
        with self.assertRaises(FrozenInstanceError):
            result.reason = "other"  # type: ignore[misc]

    def test_result_reason_records_non_persistent_application(self):
        result = apply_authorization()
        self.assertIn("without persistence", result.reason)

    def test_result_completed_at_matches_command_time(self):
        result = apply_authorization()
        authorization = result.application_request.authorization_result
        command = authorization.command
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(result.completed_at, command.issued_at)


if __name__ == "__main__":
    unittest.main()
