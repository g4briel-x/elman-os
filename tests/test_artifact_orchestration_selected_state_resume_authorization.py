from dataclasses import FrozenInstanceError, replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from elman_os.artifact_orchestration_selected_state_restoration import (
    ArtifactOrchestrationSelectedStateRestorationResult,
    ArtifactOrchestrationSelectedStateRestorationStatus,
)
from elman_os.artifact_orchestration_selected_state_resume_authorization import (
    ArtifactOrchestrationHumanResumeApproval,
    ArtifactOrchestrationSelectedStateResumeAuthorization,
    ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError,
    ArtifactOrchestrationSelectedStateResumeAuthorizationError,
    ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError,
    ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy,
    ArtifactOrchestrationSelectedStateResumeAuthorizationRequest,
    ArtifactOrchestrationSelectedStateResumeAuthorizationResult,
    ArtifactOrchestrationSelectedStateResumeAuthorizationStatus,
)
from elman_os.artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndexEntry,
    ArtifactOrchestrationStateIndexEntryStatus,
)
from elman_os.artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationResult,
    ArtifactOrchestrationRestorationStatus,
    ArtifactOrchestrationRestoredState,
)
from elman_os.artifact_orchestration_state_selection import (
    ArtifactOrchestrationStateSelectionRecord,
    ArtifactOrchestrationStateSelectionRecordDecision,
    ArtifactOrchestrationStateSelectionResult,
    ArtifactOrchestrationStateSelectionStatus,
)
from elman_os.execution_checkpoint import ExecutionCheckpoint
from elman_os.execution_journal import ExecutionEventType, ExecutionJournal
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
    StepStatus,
)
from elman_os.execution_resume import (
    ResumeDecisionStatus,
    ResumePolicy,
)


APPROVED_AT = "2026-08-06T01:00:00Z"
REQUESTED_AT = "2026-08-06T01:01:00Z"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_execution_state(kind: str = "ready", step_count: int = 1):
    if kind not in {"ready", "blocked", "terminal"}:
        raise ValueError(kind)
    if step_count < 1:
        raise ValueError(step_count)

    if kind == "ready":
        plan_status = PlanStatus.RUNNING
        step_status = StepStatus.PENDING
    elif kind == "blocked":
        plan_status = PlanStatus.BLOCKED
        step_status = StepStatus.BLOCKED
    else:
        plan_status = PlanStatus.COMPLETED
        step_status = StepStatus.COMPLETED

    steps = tuple(
        ExecutionStep(
            step_id=f"step.{index}",
            title=f"Step {index}",
            capability_id="artifact.resume",
            objective=f"Resume step {index}",
            assigned_agent_id="ELMAN_CORE",
            status=step_status,
        )
        for index in range(1, step_count + 1)
    )
    plan = ExecutionPlan(
        plan_id=f"plan:{kind}-{step_count}",
        project_id="project:resume-authorization",
        objective="Authorize a restored selected state for resume",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=plan_status,
        requires_human_approval=True,
        approval_reference=f"approval:initial-{kind}",
    )

    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        "2026-08-06T00:00:00Z",
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        "2026-08-06T00:01:00Z",
        payload={"approval_reference": plan.approval_reference},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        "2026-08-06T00:02:00Z",
        payload={"project_id": plan.project_id},
    )
    for index, step in enumerate(steps, start=1):
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            f"2026-08-06T00:0{index + 2}:00Z",
            step_id=step.step_id,
            agent_id="ELMAN_CORE",
        )

    if kind == "blocked":
        for step in steps:
            journal.append(
                ExecutionEventType.STEP_BLOCKED,
                "2026-08-06T00:10:00Z",
                step_id=step.step_id,
                agent_id="ELMAN_CORE",
                payload={"reason": "manual intervention required"},
            )
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            "2026-08-06T00:10:00Z",
            payload={"affected_step_id": steps[0].step_id},
        )
    elif kind == "terminal":
        for step in steps:
            journal.append(
                ExecutionEventType.STEP_COMPLETED,
                "2026-08-06T00:10:00Z",
                step_id=step.step_id,
                agent_id="ELMAN_CORE",
                payload={"result": "completed"},
            )
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            "2026-08-06T00:10:00Z",
            payload={"affected_step_id": steps[-1].step_id},
        )

    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id=f"checkpoint:{sha(f'{kind}:{step_count}')}",
        created_at="2026-08-06T00:20:00Z",
    )
    return plan, journal, checkpoint


def make_source_result(kind: str = "ready", step_count: int = 1):
    plan, journal, checkpoint = make_execution_state(kind, step_count)
    assessment = checkpoint.assess_resume(plan, journal)
    root = Path(tempfile.mkdtemp()).resolve()
    persistence_id = f"persistence:{sha(f'{kind}:{step_count}') }"
    storage_key = sha(persistence_id)
    state_directory = root / storage_key
    checkpoint_hash = checkpoint.checkpoint_hash
    assert checkpoint_hash is not None

    restored_state = ArtifactOrchestrationRestoredState(
        persistence_id=persistence_id,
        manifest_hash=sha(f"manifest:{kind}:{step_count}"),
        orchestration_result_hash=sha(
            f"orchestration:{kind}:{step_count}"
        ),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        checkpoint_id=checkpoint.checkpoint_id,
        plan_state_hash=sha(plan.to_json()),
        journal_hash=journal.seal().journal_hash,
        checkpoint_hash=checkpoint_hash,
        plan_json=plan.to_json(),
        journal_jsonl=journal.to_jsonl(),
        checkpoint_json=checkpoint.to_json(),
        assessment_status=assessment.status,
        can_resume=assessment.can_resume,
        assessment_json=assessment.to_json(),
        restored_at="2026-08-06T00:30:00Z",
    )
    restoration_result = ArtifactOrchestrationRestorationResult(
        restoration_id=f"restoration:{sha(kind)}",
        status=ArtifactOrchestrationRestorationStatus.RESTORED,
        request_hash=sha(f"restoration-request:{kind}"),
        policy_id="policy:restoration-source",
        policy_hash=sha("restoration-policy-source"),
        persistence_id=persistence_id,
        state_root=root,
        state_directory=state_directory,
        manifest_hash=restored_state.manifest_hash,
        orchestration_result_hash=(
            restored_state.orchestration_result_hash
        ),
        restored_state_json=restored_state.to_json(),
        completed_at="2026-08-06T00:30:00Z",
        reason="RESTORED: source fixture",
    )
    entry = ArtifactOrchestrationStateIndexEntry(
        storage_key=storage_key,
        status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
        state_directory=state_directory,
        reason_code="valid-state",
        reason="The state is valid",
        persistence_id=persistence_id,
        manifest_hash=restored_state.manifest_hash,
        orchestration_result_hash=(
            restored_state.orchestration_result_hash
        ),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        checkpoint_id=checkpoint.checkpoint_id,
        assessment_status=assessment.status,
        can_resume=assessment.can_resume,
        persisted_at="2026-08-06T00:25:00Z",
        state_hash=restored_state.state_hash,
    )
    record = ArtifactOrchestrationStateSelectionRecord(
        storage_key=storage_key,
        entry_hash=entry.entry_hash,
        entry_status=entry.status,
        decision=ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE,
        reason_codes=(),
        primary_rank="2026-08-06T00:25:00Z",
        rank_position=1,
    )
    selection_result = ArtifactOrchestrationStateSelectionResult(
        status=ArtifactOrchestrationStateSelectionStatus.SELECTED,
        request_id="selection-request:resume-authorization",
        request_hash=sha("selection-request"),
        policy_id="policy:selection-source",
        policy_hash=sha("selection-policy-source"),
        snapshot_hash=sha("selection-snapshot-source"),
        records=(record,),
        eligible_count=1,
        excluded_count=0,
        completed_at="2026-08-06T00:27:00Z",
        reason="SELECTED: source fixture",
        selected_entry_json=entry.to_json(),
        selected_record_hash=record.record_hash,
    )
    result = ArtifactOrchestrationSelectedStateRestorationResult(
        selected_restoration_id=f"selected-restoration:{sha(kind)}",
        status=ArtifactOrchestrationSelectedStateRestorationStatus.RESTORED,
        request_hash=sha("selected-restoration-request"),
        policy_id="policy:selected-restoration-source",
        policy_hash=sha("selected-restoration-policy-source"),
        selection_result_json=selection_result.to_json(),
        restoration_request_hash=restoration_result.request_hash,
        restoration_result_json=restoration_result.to_json(),
        completed_at=restoration_result.completed_at,
        reason="RESTORED: selected source fixture",
    )
    return result


def make_policy(**resume_changes):
    values = {
        "policy_id": "policy:resume-decision",
        "require_human_approval": True,
        "allowed_step_ids": (),
        "max_steps": None,
    }
    values.update(resume_changes)
    return ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy(
        policy_id="policy:selected-state-resume-authorization",
        resume_policy=ResumePolicy(**values),
    )


def make_approval(source, **changes):
    ready_steps = source.restored_state.checkpoint.assess_resume(
        source.restored_state.plan,
        source.restored_state.journal,
    ).ready_step_ids
    values = {
        "restoration_result": source,
        "approval_reference": "approval:resume-001",
        "approved_by": "human:herve",
        "approved_at": APPROVED_AT,
        "statement": "I explicitly approve resume of the listed steps.",
        "approved_step_ids": ready_steps or ("step.1",),
    }
    values.update(changes)
    return ArtifactOrchestrationHumanResumeApproval.for_restoration(**values)


def make_request(source, policy=None, approval=None, **changes):
    effective_policy = policy or make_policy()
    effective_approval = approval or make_approval(source)
    values = {
        "restoration_result": source,
        "approval": effective_approval,
        "policy": effective_policy,
        "requested_by": "ELMAN_NEXUS",
        "requested_at": REQUESTED_AT,
        "rationale": "Resume the explicitly approved restored steps.",
        "requested_step_ids": effective_approval.approved_step_ids,
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateResumeAuthorizationRequest.from_restoration_result(
        **values
    )


def authorize(source, policy=None, approval=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(
        source,
        policy=effective_policy,
        approval=approval,
        **request_changes,
    )
    return ArtifactOrchestrationSelectedStateResumeAuthorization(
        request,
        effective_policy,
    ).authorize()


class ResumeAuthorizationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(max_steps=1)
        self.assertEqual(
            ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_rejects_wrong_resume_policy_type(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy(
                policy_id="policy:selected-state-resume-authorization",
                resume_policy="invalid",  # type: ignore[arg-type]
            )

    def test_policy_requires_safety_flags(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy(
                policy_id="policy:selected-state-resume-authorization",
                resume_policy=ResumePolicy(
                    policy_id="policy:resume-decision"
                ),
                require_explicit_step_scope=False,
            )

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class HumanResumeApprovalTests(unittest.TestCase):
    def test_approval_identifier_is_deterministic(self):
        source = make_source_result()
        self.assertEqual(
            make_approval(source).approval_id,
            make_approval(source).approval_id,
        )

    def test_approval_json_round_trip(self):
        approval = make_approval(make_source_result())
        restored = ArtifactOrchestrationHumanResumeApproval.from_json(
            approval.to_json()
        )
        self.assertEqual(restored, approval)
        restored.verify_hash()

    def test_approval_rejects_empty_scope(self):
        source = make_source_result()
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationError
        ):
            make_approval(source, approved_step_ids=())

    def test_approval_rejects_tampered_hash(self):
        approval = make_approval(make_source_result())
        data = approval.to_dict()
        data["statement"] = "Tampered"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError
        ):
            ArtifactOrchestrationHumanResumeApproval.from_dict(data)

    def test_approval_is_frozen(self):
        approval = make_approval(make_source_result())
        with self.assertRaises(FrozenInstanceError):
            approval.statement = "other"  # type: ignore[misc]


class ResumeAuthorizationRequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        source = make_source_result()
        self.assertEqual(
            make_request(source).authorization_id,
            make_request(source).authorization_id,
        )

    def test_request_json_round_trip(self):
        request = make_request(make_source_result())
        restored = ArtifactOrchestrationSelectedStateResumeAuthorizationRequest.from_json(
            request.to_json()
        )
        self.assertEqual(restored, request)
        restored.verify_hash()

    def test_request_rejects_steps_outside_approval_scope(self):
        source = make_source_result(step_count=2)
        approval = make_approval(
            source,
            approved_step_ids=("step.1",),
        )
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError
        ):
            make_request(
                source,
                approval=approval,
                requested_step_ids=("step.2",),
            )

    def test_request_rejects_time_before_approval(self):
        source = make_source_result()
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError
        ):
            make_request(
                source,
                requested_at="2026-08-06T00:59:00Z",
            )

    def test_request_rejects_approval_for_other_restoration(self):
        source = make_source_result("ready", 1)
        other = make_source_result("ready", 2)
        approval = make_approval(other)
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationApprovalError
        ):
            make_request(source, approval=approval)

    def test_request_rejects_tampered_hash(self):
        request = make_request(make_source_result())
        data = request.to_dict()
        data["rationale"] = "Tampered"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorizationRequest.from_dict(
                data
            )


class SelectedStateResumeAuthorizationTests(unittest.TestCase):
    def test_ready_state_is_approved(self):
        result = authorize(make_source_result("ready"))
        self.assertIs(
            result.status,
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.APPROVED,
        )
        self.assertIs(result.decision.status, ResumeDecisionStatus.APPROVED)
        self.assertIsNotNone(result.command)
        self.assertEqual(result.decision.selected_step_ids, ("step.1",))

    def test_blocked_state_is_rejected(self):
        result = authorize(make_source_result("blocked"))
        self.assertIs(
            result.status,
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.REJECTED,
        )
        self.assertIs(result.decision.status, ResumeDecisionStatus.REJECTED)
        self.assertIsNone(result.command)

    def test_terminal_state_is_rejected(self):
        result = authorize(make_source_result("terminal"))
        self.assertIs(
            result.status,
            ArtifactOrchestrationSelectedStateResumeAuthorizationStatus.REJECTED,
        )
        self.assertIsNone(result.command)

    def test_max_steps_limits_approved_command(self):
        source = make_source_result("ready", 2)
        policy = make_policy(max_steps=1)
        result = authorize(source, policy=policy)
        self.assertEqual(result.decision.selected_step_ids, ("step.1",))

    def test_policy_allowed_steps_filters_selection(self):
        source = make_source_result("ready", 2)
        policy = make_policy(allowed_step_ids=("step.2",))
        result = authorize(
            source,
            policy=policy,
            requested_step_ids=("step.2",),
        )
        self.assertEqual(result.decision.selected_step_ids, ("step.2",))

    def test_unavailable_requested_step_produces_rejection(self):
        source = make_source_result("ready", 2)
        policy = make_policy(allowed_step_ids=("step.1",))
        result = authorize(
            source,
            policy=policy,
            requested_step_ids=("step.2",),
        )
        self.assertIs(result.decision.status, ResumeDecisionStatus.REJECTED)

    def test_authorization_is_deterministic(self):
        source = make_source_result("ready", 2)
        approval = make_approval(source)
        first = authorize(source, approval=approval)
        second = authorize(source, approval=approval)
        self.assertEqual(first, second)
        self.assertEqual(first.result_hash, second.result_hash)

    def test_authorization_does_not_mutate_source(self):
        source = make_source_result("ready", 2)
        before = source.to_json()
        result = authorize(source)
        self.assertEqual(source.to_json(), before)
        self.assertNotEqual(result.result_hash, source.result_hash)

    def test_command_binds_explicit_approval_reference(self):
        source = make_source_result()
        approval = make_approval(
            source,
            approval_reference="approval:resume-explicit",
        )
        result = authorize(source, approval=approval)
        assert result.command is not None
        self.assertEqual(
            result.command.approval_reference,
            "approval:resume-explicit",
        )

    def test_result_json_round_trip(self):
        result = authorize(make_source_result())
        restored = ArtifactOrchestrationSelectedStateResumeAuthorizationResult.from_json(
            result.to_json()
        )
        self.assertEqual(restored, result)
        restored.verify_hash()

    def test_result_rejects_tampered_decision(self):
        result = authorize(make_source_result())
        data = result.to_dict()
        data["reason"] = "Tampered"
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorizationResult.from_dict(
                data
            )

    def test_result_rejects_status_mismatch(self):
        result = authorize(make_source_result())
        data = result.to_dict()
        data["status"] = "rejected"
        data["result_hash"] = None
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorizationResult(
                authorization_id=data["authorization_id"],
                status=data["status"],
                authorization_request_json=data[
                    "authorization_request_json"
                ],
                resume_request_json=data["resume_request_json"],
                assessment_json=data["assessment_json"],
                decision_json=data["decision_json"],
                completed_at=data["completed_at"],
                reason=data["reason"],
            )

    def test_executor_rejects_mismatched_policy(self):
        source = make_source_result()
        request = make_request(source, policy=make_policy())
        other = ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy(
            policy_id="policy:other-authorization",
            resume_policy=ResumePolicy(policy_id="policy:other-resume"),
        )
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumeAuthorizationError
        ):
            ArtifactOrchestrationSelectedStateResumeAuthorization(
                request,
                other,
            )

    def test_approval_scope_is_preserved_in_command(self):
        source = make_source_result("ready", 2)
        approval = make_approval(
            source,
            approved_step_ids=("step.2",),
        )
        result = authorize(
            source,
            approval=approval,
            requested_step_ids=("step.2",),
        )
        self.assertEqual(result.decision.selected_step_ids, ("step.2",))


if __name__ == "__main__":
    unittest.main()
