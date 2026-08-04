from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.execution_checkpoint import (
    ExecutionCheckpoint,
    ResumeAssessment,
    ResumeAssessmentStatus,
)
from elman_os.execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
)
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
    StepStatus,
)
from elman_os.execution_resume import (
    ExecutionResumeError,
    ResumeCommand,
    ResumeDecision,
    ResumeDecisionStatus,
    ResumeIntegrityError,
    ResumePolicy,
    ResumeRequest,
    ResumeSelectionStrategy,
    decide_resume,
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
T3 = "2026-08-04T00:00:03Z"
T4 = "2026-08-04T00:00:04Z"
T5 = "2026-08-04T00:00:05Z"
ISSUED = "2026-08-04T00:10:00Z"


def make_step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    assigned_agent_id: str | None = None,
    status: StepStatus = StepStatus.PENDING,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        title=f"Title {step_id}",
        capability_id="build",
        objective=f"Objective {step_id}",
        dependencies=dependencies,
        required_permissions=("build",),
        assigned_agent_id=assigned_agent_id,
        status=status,
    )


def make_plan(
    steps: tuple[ExecutionStep, ...],
    *,
    status: PlanStatus = PlanStatus.PENDING,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Build safely",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=status,
        requires_human_approval=False,
    )


def pending_context(
    step_ids: tuple[str, ...] = ("step.one", "step.two"),
) -> tuple[
    ExecutionPlan,
    ExecutionJournal,
    ExecutionCheckpoint,
    ResumeAssessment,
]:
    current_plan = make_plan(
        tuple(make_step(step_id) for step_id in step_ids)
    )
    journal = ExecutionJournal(current_plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
    )
    checkpoint = ExecutionCheckpoint.capture(
        current_plan,
        journal,
        checkpoint_id="checkpoint:001",
        created_at="2026-08-04T00:01:00Z",
    )
    assessment = checkpoint.assess_resume(current_plan, journal)
    return current_plan, journal, checkpoint, assessment


def blocked_context() -> tuple[
    ExecutionPlan,
    ExecutionJournal,
    ExecutionCheckpoint,
    ResumeAssessment,
]:
    blocked = make_step(
        "step.one",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.BLOCKED,
    )
    current_plan = make_plan(
        (blocked,),
        status=PlanStatus.BLOCKED,
    )
    journal = ExecutionJournal(current_plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        T1,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.STEP_READY,
        T2,
        step_id="step.one",
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        T3,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        T4,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_BLOCKED,
        T5,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.PLAN_BLOCKED,
        "2026-08-04T00:00:06Z",
        agent_id="ELMAN_NEXUS",
    )
    checkpoint = ExecutionCheckpoint.capture(
        current_plan,
        journal,
        checkpoint_id="checkpoint:blocked",
        created_at="2026-08-04T00:01:00Z",
    )
    assessment = checkpoint.assess_resume(current_plan, journal)
    return current_plan, journal, checkpoint, assessment


def completed_context() -> tuple[
    ExecutionPlan,
    ExecutionJournal,
    ExecutionCheckpoint,
    ResumeAssessment,
]:
    completed = make_step(
        "step.one",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.COMPLETED,
    )
    current_plan = make_plan(
        (completed,),
        status=PlanStatus.COMPLETED,
    )
    journal = ExecutionJournal(current_plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        T1,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.STEP_READY,
        T2,
        step_id="step.one",
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        T3,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        T4,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_COMPLETED,
        T5,
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.PLAN_COMPLETED,
        "2026-08-04T00:00:06Z",
        agent_id="ELMAN_NEXUS",
    )
    checkpoint = ExecutionCheckpoint.capture(
        current_plan,
        journal,
        checkpoint_id="checkpoint:completed",
        created_at="2026-08-04T00:01:00Z",
    )
    assessment = checkpoint.assess_resume(current_plan, journal)
    return current_plan, journal, checkpoint, assessment


def make_policy(
    *,
    allowed_step_ids: tuple[str, ...] = (),
    max_steps: int | None = None,
) -> ResumePolicy:
    return ResumePolicy(
        policy_id="policy:controlled-resume",
        allowed_step_ids=allowed_step_ids,
        max_steps=max_steps,
    )


def make_request(
    checkpoint: ExecutionCheckpoint,
    *,
    checkpoint_id: str | None = None,
    checkpoint_hash: str | None = None,
    plan_id: str | None = None,
    requested_step_ids: tuple[str, ...] = (),
    approval_reference: str = "approval:001",
) -> ResumeRequest:
    assert checkpoint.checkpoint_hash is not None
    return ResumeRequest(
        request_id="request:001",
        checkpoint_id=checkpoint_id or checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint_hash or checkpoint.checkpoint_hash,
        plan_id=plan_id or checkpoint.plan_id,
        requested_by="ELMAN_NEXUS",
        approval_reference=approval_reference,
        created_at="2026-08-04T00:05:00Z",
        rationale="Operator reviewed the checkpoint and authorizes resume",
        requested_step_ids=requested_step_ids,
    )


class ResumePolicyTests(unittest.TestCase):
    def test_policy_defaults_to_ready_only(self) -> None:
        policy = make_policy()

        self.assertIs(
            policy.strategy,
            ResumeSelectionStrategy.READY_ONLY,
        )
        self.assertTrue(policy.require_human_approval)

    def test_policy_rejects_disabled_approval(self) -> None:
        with self.assertRaises(ExecutionResumeError):
            ResumePolicy(
                policy_id="policy:unsafe",
                require_human_approval=False,
            )

    def test_policy_normalizes_allowed_steps(self) -> None:
        policy = make_policy(
            allowed_step_ids=("step.two", "step.one", "step.two"),
        )

        self.assertEqual(
            policy.allowed_step_ids,
            ("step.one", "step.two"),
        )

    def test_policy_rejects_invalid_max_steps(self) -> None:
        with self.assertRaises(ExecutionResumeError):
            make_policy(max_steps=0)

    def test_policy_json_round_trip(self) -> None:
        original = make_policy(
            allowed_step_ids=("step.one",),
            max_steps=1,
        )

        restored = ResumePolicy.from_json(original.to_json())

        self.assertEqual(restored, original)
        self.assertEqual(restored.policy_hash, original.policy_hash)

    def test_policy_hash_is_deterministic(self) -> None:
        left = make_policy(allowed_step_ids=("step.two", "step.one"))
        right = make_policy(allowed_step_ids=("step.one", "step.two"))

        self.assertEqual(left.policy_hash, right.policy_hash)


class ResumeRequestTests(unittest.TestCase):
    def test_request_requires_approval_reference(self) -> None:
        _, _, checkpoint, _ = pending_context()

        with self.assertRaises(ExecutionResumeError):
            make_request(checkpoint, approval_reference="")

    def test_request_normalizes_requested_steps(self) -> None:
        _, _, checkpoint, _ = pending_context()
        request = make_request(
            checkpoint,
            requested_step_ids=("step.two", "step.one", "step.two"),
        )

        self.assertEqual(
            request.requested_step_ids,
            ("step.one", "step.two"),
        )

    def test_request_rejects_invalid_checkpoint_hash(self) -> None:
        _, _, checkpoint, _ = pending_context()

        with self.assertRaises(ExecutionResumeError):
            make_request(checkpoint, checkpoint_hash="invalid")

    def test_request_rejects_non_utc_datetime(self) -> None:
        _, _, checkpoint, _ = pending_context()
        assert checkpoint.checkpoint_hash is not None

        with self.assertRaises(ExecutionResumeError):
            ResumeRequest(
                request_id="request:001",
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_hash=checkpoint.checkpoint_hash,
                plan_id=checkpoint.plan_id,
                requested_by="ELMAN_NEXUS",
                approval_reference="approval:001",
                created_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
                rationale="Reviewed",
            )

    def test_request_accepts_utc_datetime(self) -> None:
        _, _, checkpoint, _ = pending_context()
        assert checkpoint.checkpoint_hash is not None
        request = ResumeRequest(
            request_id="request:001",
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_hash=checkpoint.checkpoint_hash,
            plan_id=checkpoint.plan_id,
            requested_by="ELMAN_NEXUS",
            approval_reference="approval:001",
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
            rationale="Reviewed",
        )

        self.assertEqual(
            request.created_at,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_request_json_round_trip(self) -> None:
        _, _, checkpoint, _ = pending_context()
        original = make_request(
            checkpoint,
            requested_step_ids=("step.one",),
        )

        restored = ResumeRequest.from_json(original.to_json())

        self.assertEqual(restored, original)
        self.assertEqual(restored.request_hash, original.request_hash)

    def test_request_is_frozen(self) -> None:
        _, _, checkpoint, _ = pending_context()
        request = make_request(checkpoint)

        with self.assertRaises(FrozenInstanceError):
            request.plan_id = "plan:other"  # type: ignore[misc]


class ResumeCommandAndDecisionTests(unittest.TestCase):
    def test_approved_decision_contains_hashed_command(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        request = make_request(checkpoint)
        decision = decide_resume(
            request,
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.APPROVED)
        self.assertIsNotNone(decision.command)
        assert decision.command is not None
        self.assertEqual(len(decision.command.command_hash or ""), 64)
        decision.verify_hash()

    def test_command_json_round_trip(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )
        assert decision.command is not None

        restored = ResumeCommand.from_json(
            decision.command.to_json()
        )

        self.assertEqual(restored, decision.command)
        restored.verify_hash()

    def test_decision_json_round_trip(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        original = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        restored = ResumeDecision.from_json(original.to_json())

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_tampered_command_is_detected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )
        assert decision.command is not None
        data = decision.command.to_dict()
        data["selected_step_ids"] = ["step.other"]

        with self.assertRaises(ResumeIntegrityError):
            ResumeCommand.from_dict(data)

    def test_tampered_decision_is_detected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )
        data = decision.to_dict()
        data["reasons"] = ["tampered"]

        with self.assertRaises(ResumeIntegrityError):
            ResumeDecision.from_dict(data)

    def test_rejected_decision_has_no_command(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        decision = decide_resume(
            make_request(
                checkpoint,
                requested_step_ids=("step.missing",),
            ),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)
        self.assertIsNone(decision.command)
        self.assertEqual(decision.selected_step_ids, ())

    def test_approved_decision_requires_command(self) -> None:
        _, _, checkpoint, _ = pending_context()
        assert checkpoint.checkpoint_hash is not None

        with self.assertRaises(ExecutionResumeError):
            ResumeDecision(
                decision_id="decision:001",
                request_id="request:001",
                request_hash="1" * 64,
                policy_id="policy:001",
                policy_hash="2" * 64,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_hash=checkpoint.checkpoint_hash,
                status=ResumeDecisionStatus.APPROVED,
                reasons=("approved",),
                selected_step_ids=("step.one",),
                issued_at=ISSUED,
                command=None,
            )

    def test_rejected_decision_rejects_selected_steps(self) -> None:
        _, _, checkpoint, _ = pending_context()
        assert checkpoint.checkpoint_hash is not None

        with self.assertRaises(ExecutionResumeError):
            ResumeDecision(
                decision_id="decision:001",
                request_id="request:001",
                request_hash="1" * 64,
                policy_id="policy:001",
                policy_hash="2" * 64,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_hash=checkpoint.checkpoint_hash,
                status=ResumeDecisionStatus.REJECTED,
                reasons=("rejected",),
                selected_step_ids=("step.one",),
                issued_at=ISSUED,
                command=None,
            )


class ResumeDecisionEngineTests(unittest.TestCase):
    def test_ready_assessment_approves_all_ready_steps(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.APPROVED)
        self.assertEqual(
            decision.selected_step_ids,
            ("step.one", "step.two"),
        )

    def test_requested_subset_is_selected_deterministically(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(
                checkpoint,
                requested_step_ids=("step.two",),
            ),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertEqual(decision.selected_step_ids, ("step.two",))

    def test_policy_allowed_steps_filters_selection(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(allowed_step_ids=("step.two",)),
            issued_at=ISSUED,
        )

        self.assertEqual(decision.selected_step_ids, ("step.two",))

    def test_policy_max_steps_applies_lexically(self) -> None:
        _, _, checkpoint, assessment = pending_context(
            ("step.c", "step.a", "step.b")
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(max_steps=2),
            issued_at=ISSUED,
        )

        self.assertEqual(
            decision.selected_step_ids,
            ("step.a", "step.b"),
        )

    def test_requested_step_outside_policy_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(
                checkpoint,
                requested_step_ids=("step.one",),
            ),
            checkpoint,
            assessment,
            make_policy(allowed_step_ids=("step.two",)),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_checkpoint_identifier_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(
                checkpoint,
                checkpoint_id="checkpoint:other",
            ),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_checkpoint_hash_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(
                checkpoint,
                checkpoint_hash="1" * 64,
            ),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_plan_identifier_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        decision = decide_resume(
            make_request(
                checkpoint,
                plan_id="plan:other",
            ),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_assessment_checkpoint_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        foreign = ResumeAssessment(
            checkpoint_id="checkpoint:other",
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=assessment.ready_step_ids,
            running_step_ids=(),
            current_event_count=assessment.current_event_count,
            current_head_hash=assessment.current_head_hash,
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            foreign,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_stale_assessment_is_rejected(self) -> None:
        old_plan, journal, checkpoint, _ = pending_context(
            ("step.one",)
        )
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            agent_id="ELMAN_NEXUS",
        )
        journal.append(
            ExecutionEventType.STEP_READY,
            T2,
            step_id="step.one",
        )
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            T3,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )
        journal.append(
            ExecutionEventType.STEP_STARTED,
            T4,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )
        current_plan = make_plan(
            (
                make_step(
                    "step.one",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.RUNNING,
                ),
            ),
            status=PlanStatus.RUNNING,
        )
        stale = checkpoint.assess_resume(current_plan, journal)

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            stale,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(stale.status, ResumeAssessmentStatus.STALE)
        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_blocked_checkpoint_is_rejected(self) -> None:
        _, _, checkpoint, assessment = blocked_context()

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_terminal_checkpoint_is_rejected(self) -> None:
        _, _, checkpoint, assessment = completed_context()

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_assessment_event_count_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        forged = ResumeAssessment(
            checkpoint_id=assessment.checkpoint_id,
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=assessment.ready_step_ids,
            running_step_ids=(),
            current_event_count=assessment.current_event_count + 1,
            current_head_hash=assessment.current_head_hash,
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            forged,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_assessment_head_mismatch_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        forged = ResumeAssessment(
            checkpoint_id=assessment.checkpoint_id,
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=assessment.ready_step_ids,
            running_step_ids=(),
            current_event_count=assessment.current_event_count,
            current_head_hash="1" * 64,
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            forged,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_unknown_ready_step_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        forged = ResumeAssessment(
            checkpoint_id=assessment.checkpoint_id,
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=("step.missing",),
            running_step_ids=(),
            current_event_count=assessment.current_event_count,
            current_head_hash=assessment.current_head_hash,
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            forged,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_running_step_forged_as_ready_is_rejected(self) -> None:
        current_plan = make_plan(
            (
                make_step(
                    "step.one",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.RUNNING,
                ),
            ),
            status=PlanStatus.RUNNING,
        )
        journal = ExecutionJournal(current_plan.plan_id)
        journal.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            agent_id="ELMAN_NEXUS",
        )
        journal.append(
            ExecutionEventType.STEP_READY,
            T2,
            step_id="step.one",
        )
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            T3,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )
        journal.append(
            ExecutionEventType.STEP_STARTED,
            T4,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )
        checkpoint = ExecutionCheckpoint.capture(
            current_plan,
            journal,
            checkpoint_id="checkpoint:running",
            created_at="2026-08-04T00:01:00Z",
        )
        valid = checkpoint.assess_resume(current_plan, journal)
        forged = ResumeAssessment(
            checkpoint_id=valid.checkpoint_id,
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=("step.one",),
            running_step_ids=("step.one",),
            current_event_count=valid.current_event_count,
            current_head_hash=valid.current_head_hash,
        )

        decision = decide_resume(
            make_request(checkpoint),
            checkpoint,
            forged,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertIs(decision.status, ResumeDecisionStatus.REJECTED)

    def test_decision_does_not_mutate_inputs(self) -> None:
        current_plan, journal, checkpoint, assessment = pending_context()
        plan_json = current_plan.to_json()
        journal_jsonl = journal.to_jsonl()
        checkpoint_json = checkpoint.to_json()
        assessment_json = assessment.to_json()

        decide_resume(
            make_request(checkpoint),
            checkpoint,
            assessment,
            make_policy(),
            issued_at=ISSUED,
        )

        self.assertEqual(current_plan.to_json(), plan_json)
        self.assertEqual(journal.to_jsonl(), journal_jsonl)
        self.assertEqual(checkpoint.to_json(), checkpoint_json)
        self.assertEqual(assessment.to_json(), assessment_json)

    def test_decision_is_deterministic(self) -> None:
        _, _, checkpoint, assessment = pending_context()
        request = make_request(checkpoint)
        policy = make_policy()

        first = decide_resume(
            request,
            checkpoint,
            assessment,
            policy,
            issued_at=ISSUED,
        )
        second = decide_resume(
            request,
            checkpoint,
            assessment,
            policy,
            issued_at=ISSUED,
        )

        self.assertEqual(first.to_json(), second.to_json())

    def test_invalid_issued_at_is_rejected(self) -> None:
        _, _, checkpoint, assessment = pending_context()

        with self.assertRaises(ExecutionResumeError):
            decide_resume(
                make_request(checkpoint),
                checkpoint,
                assessment,
                make_policy(),
                issued_at="2026-08-04T00:10:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
