from dataclasses import FrozenInstanceError, replace
import json
import unittest

from elman_os.execution_checkpoint import (
    ExecutionCheckpoint,
    ResumeAssessmentStatus,
)
from elman_os.execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
    JournalTimestampError,
)
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
    StepStatus,
)
from elman_os.execution_resume import (
    ResumeCommand,
    ResumePolicy,
    ResumeRequest,
    decide_resume,
)
from elman_os.resume_application import (
    ResumeApplication,
    ResumeApplicationConflictError,
    ResumeApplicationError,
    ResumeApplicationIntegrityError,
    ResumeApplicationResult,
    ResumeApplicationStatus,
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
T3 = "2026-08-04T00:00:03Z"
T4 = "2026-08-04T00:00:04Z"
T5 = "2026-08-04T00:00:05Z"
T6 = "2026-08-04T00:00:06Z"
ISSUED = "2026-08-04T00:10:00Z"


def make_step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    assigned_agent_id: str | None = None,
    status: StepStatus = StepStatus.PENDING,
    approval_reference: str | None = None,
    requires_human_approval: bool = False,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        title=f"Title {step_id}",
        capability_id="build",
        objective=f"Objective {step_id}",
        dependencies=dependencies,
        required_permissions=("build",),
        assigned_agent_id=assigned_agent_id,
        requires_human_approval=requires_human_approval,
        approval_reference=approval_reference,
        status=status,
    )


def make_plan(
    steps: tuple[ExecutionStep, ...],
    *,
    plan_id: str = "plan:001",
    status: PlanStatus = PlanStatus.PENDING,
    approval_reference: str | None = None,
    requires_human_approval: bool = False,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        project_id="project:001",
        objective="Build safely",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=status,
        requires_human_approval=requires_human_approval,
        approval_reference=approval_reference,
    )


def pending_context(
    *,
    requires_human_approval: bool = False,
) -> tuple[
    ExecutionPlan,
    ExecutionJournal,
    ExecutionCheckpoint,
]:
    current_plan = make_plan(
        (
            make_step(
                "step.one",
                requires_human_approval=requires_human_approval,
            ),
            make_step("step.two"),
        ),
        requires_human_approval=requires_human_approval,
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
    return current_plan, journal, checkpoint


def running_context() -> tuple[
    ExecutionPlan,
    ExecutionJournal,
    ExecutionCheckpoint,
]:
    root = make_step(
        "step.root",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.COMPLETED,
    )
    final = make_step(
        "step.final",
        dependencies=("step.root",),
    )
    current_plan = make_plan(
        (root, final),
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
        step_id="step.root",
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        T3,
        step_id="step.root",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        T4,
        step_id="step.root",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_COMPLETED,
        T5,
        step_id="step.root",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_READY,
        T6,
        step_id="step.final",
    )
    checkpoint = ExecutionCheckpoint.capture(
        current_plan,
        journal,
        checkpoint_id="checkpoint:running",
        created_at="2026-08-04T00:01:00Z",
    )
    return current_plan, journal, checkpoint


def command_for(
    plan: ExecutionPlan,
    journal: ExecutionJournal,
    checkpoint: ExecutionCheckpoint,
    *,
    requested_step_ids: tuple[str, ...] = (),
    approval_reference: str = "approval:resume-001",
    issued_at: str = ISSUED,
) -> ResumeCommand:
    assessment = checkpoint.assess_resume(plan, journal)
    request = ResumeRequest(
        request_id="request:resume-001",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint.checkpoint_hash or "",
        plan_id=checkpoint.plan_id,
        requested_by="ELMAN_NEXUS",
        approval_reference=approval_reference,
        created_at="2026-08-04T00:05:00Z",
        rationale="Human operator approved controlled resume",
        requested_step_ids=requested_step_ids,
    )
    decision = decide_resume(
        request,
        checkpoint,
        assessment,
        ResumePolicy(policy_id="policy:resume-001"),
        issued_at=issued_at,
    )
    assert decision.command is not None
    return decision.command


class ResumeApplicationConstructionTests(unittest.TestCase):
    def test_application_accepts_matching_command_and_checkpoint(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        application = ResumeApplication(command, checkpoint)

        self.assertTrue(application.application_id.startswith("application:"))

    def test_application_rejects_checkpoint_identifier_mismatch(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        foreign = replace(
            checkpoint,
            checkpoint_id="checkpoint:other",
            checkpoint_hash=None,
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(command, foreign)

    def test_application_rejects_checkpoint_hash_mismatch(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        forged = ResumeCommand(
            command_id=command.command_id,
            request_id=command.request_id,
            request_hash=command.request_hash,
            policy_id=command.policy_id,
            policy_hash=command.policy_hash,
            checkpoint_id=command.checkpoint_id,
            checkpoint_hash="1" * 64,
            plan_id=command.plan_id,
            approval_reference=command.approval_reference,
            selected_step_ids=command.selected_step_ids,
            issued_at=command.issued_at,
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(forged, checkpoint)

    def test_application_rejects_plan_identifier_mismatch(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        forged = ResumeCommand(
            command_id=command.command_id,
            request_id=command.request_id,
            request_hash=command.request_hash,
            policy_id=command.policy_id,
            policy_hash=command.policy_hash,
            checkpoint_id=command.checkpoint_id,
            checkpoint_hash=command.checkpoint_hash,
            plan_id="plan:other",
            approval_reference=command.approval_reference,
            selected_step_ids=command.selected_step_ids,
            issued_at=command.issued_at,
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(forged, checkpoint)

    def test_application_is_frozen(self) -> None:
        plan, journal, checkpoint = pending_context()
        application = ResumeApplication(
            command_for(plan, journal, checkpoint),
            checkpoint,
        )

        with self.assertRaises(FrozenInstanceError):
            application.checkpoint = checkpoint  # type: ignore[misc]


class PendingPlanApplicationTests(unittest.TestCase):
    def test_application_approves_pending_plan_and_steps(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertIs(result.status, ResumeApplicationStatus.APPLIED)
        self.assertIs(result.updated_plan.status, PlanStatus.APPROVED)
        self.assertEqual(
            tuple(step.status for step in result.updated_plan.steps),
            (StepStatus.APPROVED, StepStatus.APPROVED),
        )
        self.assertEqual(
            result.updated_plan.approval_reference,
            command.approval_reference,
        )

    def test_application_preserves_human_approval_reference(self) -> None:
        plan, journal, checkpoint = pending_context(
            requires_human_approval=True
        )
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertEqual(
            result.updated_plan.get_step("step.one").approval_reference,
            command.approval_reference,
        )
        self.assertEqual(
            result.updated_plan.approval_reference,
            command.approval_reference,
        )

    def test_application_appends_plan_then_step_markers(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)
        appended = result.updated_events[-3:]

        self.assertEqual(
            tuple(event.event_type for event in appended),
            (
                ExecutionEventType.PLAN_APPROVED,
                ExecutionEventType.STEP_APPROVED,
                ExecutionEventType.STEP_APPROVED,
            ),
        )
        self.assertEqual(
            tuple(event.step_id for event in appended[1:]),
            ("step.one", "step.two"),
        )

    def test_application_marker_payload_links_command_and_checkpoint(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)
        event = result.updated_events[-1]

        self.assertEqual(
            event.payload["resume_command_hash"],
            command.command_hash,
        )
        self.assertEqual(
            event.payload["resume_checkpoint_hash"],
            checkpoint.checkpoint_hash,
        )
        self.assertEqual(
            event.payload["resume_approval_reference"],
            command.approval_reference,
        )

    def test_application_advances_journal_contiguously(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertEqual(result.appended_event_sequences, (2, 3, 4))
        self.assertEqual(result.journal_before_event_count, 1)
        self.assertEqual(result.journal_after_event_count, 4)

    def test_application_returns_compatible_plan_and_journal(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        validation = ExecutionCheckpoint.capture(
            result.updated_plan,
            result.to_journal(),
            checkpoint_id="checkpoint:post-application",
            created_at=ISSUED,
        )
        self.assertEqual(validation.plan_id, plan.plan_id)

    def test_application_does_not_mutate_inputs(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        plan_json = plan.to_json()
        journal_jsonl = journal.to_jsonl()

        ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertEqual(plan.to_json(), plan_json)
        self.assertEqual(journal.to_jsonl(), journal_jsonl)

    def test_application_is_deterministic(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        application = ResumeApplication(command, checkpoint)

        first = application.apply(plan, journal)
        second = application.apply(plan, journal)

        self.assertEqual(first.to_json(), second.to_json())

    def test_application_can_select_a_subset(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(
            plan,
            journal,
            checkpoint,
            requested_step_ids=("step.two",),
        )

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertIs(
            result.updated_plan.get_step("step.one").status,
            StepStatus.PENDING,
        )
        self.assertIs(
            result.updated_plan.get_step("step.two").status,
            StepStatus.APPROVED,
        )
        self.assertEqual(result.appended_event_sequences, (2, 3))


class RunningPlanApplicationTests(unittest.TestCase):
    def test_running_plan_remains_running(self) -> None:
        plan, journal, checkpoint = running_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        self.assertIs(result.updated_plan.status, PlanStatus.RUNNING)
        self.assertIs(
            result.updated_plan.get_step("step.final").status,
            StepStatus.APPROVED,
        )

    def test_running_plan_does_not_append_plan_approved(self) -> None:
        plan, journal, checkpoint = running_context()
        command = command_for(plan, journal, checkpoint)

        result = ResumeApplication(command, checkpoint).apply(plan, journal)

        appended = result.updated_events[journal.event_count:]
        self.assertEqual(len(appended), 1)
        self.assertIs(
            appended[0].event_type,
            ExecutionEventType.STEP_APPROVED,
        )


class IdempotencyTests(unittest.TestCase):
    def test_second_application_is_already_applied(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        application = ResumeApplication(command, checkpoint)
        first = application.apply(plan, journal)

        second = application.apply(
            first.updated_plan,
            first.to_journal(),
        )

        self.assertIs(
            second.status,
            ResumeApplicationStatus.ALREADY_APPLIED,
        )
        self.assertEqual(second.appended_event_sequences, ())
        self.assertEqual(
            second.journal_after_event_count,
            first.journal_after_event_count,
        )

    def test_already_applied_result_does_not_change_state(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        application = ResumeApplication(command, checkpoint)
        first = application.apply(plan, journal)
        current_journal = first.to_journal()

        second = application.apply(first.updated_plan, current_journal)

        self.assertEqual(
            second.updated_plan.to_json(),
            first.updated_plan.to_json(),
        )
        self.assertEqual(
            second.to_journal().to_jsonl(),
            current_journal.to_jsonl(),
        )

    def test_same_command_id_with_different_hash_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        conflicting = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events,
        )
        conflicting.append(
            ExecutionEventType.PLAN_APPROVED,
            ISSUED,
            payload={
                "resume_command_id": command.command_id,
                "resume_command_hash": "1" * 64,
            },
        )

        with self.assertRaises(ResumeApplicationConflictError):
            ResumeApplication(command, checkpoint).apply(
                plan,
                conflicting,
            )

    def test_partial_application_markers_are_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        partial = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events,
        )
        partial.append(
            ExecutionEventType.PLAN_APPROVED,
            ISSUED,
            payload={
                "resume_application_id": (
                    f"application:{command.command_hash}"
                ),
                "resume_command_id": command.command_id,
                "resume_command_hash": command.command_hash,
                "resume_checkpoint_id": command.checkpoint_id,
                "resume_checkpoint_hash": command.checkpoint_hash,
                "resume_approval_reference": (
                    command.approval_reference
                ),
                "resume_selected_step_ids": list(
                    command.selected_step_ids
                ),
            },
        )

        with self.assertRaises(ResumeApplicationIntegrityError):
            ResumeApplication(command, checkpoint).apply(plan, partial)


class RejectionTests(unittest.TestCase):
    def test_stale_checkpoint_without_application_markers_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        advanced = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events,
        )
        advanced.append(
            ExecutionEventType.PLAN_APPROVED,
            ISSUED,
            payload={"unrelated": True},
        )
        approved_plan = replace(
            plan,
            status=PlanStatus.APPROVED,
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(command, checkpoint).apply(
                approved_plan,
                advanced,
            )

    def test_plan_identifier_mismatch_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        foreign_plan = make_plan(
            plan.steps,
            plan_id="plan:other",
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(command, checkpoint).apply(
                foreign_plan,
                journal,
            )

    def test_journal_identifier_mismatch_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        foreign = ExecutionJournal("plan:other")
        foreign.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(command, checkpoint).apply(plan, foreign)

    def test_non_ready_selected_step_is_rejected(self) -> None:
        root = make_step(
            "step.root",
            assigned_agent_id="ELMAN_CORE",
            status=StepStatus.COMPLETED,
        )
        current_plan = make_plan(
            (root,),
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
            step_id="step.root",
        )
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            T3,
            step_id="step.root",
            agent_id="ELMAN_CORE",
        )
        journal.append(
            ExecutionEventType.STEP_STARTED,
            T4,
            step_id="step.root",
            agent_id="ELMAN_CORE",
        )
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T5,
            step_id="step.root",
            agent_id="ELMAN_CORE",
        )
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            T6,
            agent_id="ELMAN_NEXUS",
        )
        checkpoint = ExecutionCheckpoint.capture(
            current_plan,
            journal,
            checkpoint_id="checkpoint:terminal",
            created_at=ISSUED,
        )
        command = ResumeCommand(
            command_id="command:forged",
            request_id="request:forged",
            request_hash="1" * 64,
            policy_id="policy:forged",
            policy_hash="2" * 64,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_hash=checkpoint.checkpoint_hash or "",
            plan_id=checkpoint.plan_id,
            approval_reference="approval:forged",
            selected_step_ids=("step.root",),
            issued_at=ISSUED,
        )

        with self.assertRaises(ResumeApplicationError):
            ResumeApplication(command, checkpoint).apply(
                current_plan,
                journal,
            )

    def test_conflicting_existing_step_approval_is_rejected(self) -> None:
        approved_step = make_step(
            "step.one",
            status=StepStatus.APPROVED,
            approval_reference="approval:old",
        )
        plan = make_plan(
            (approved_step,),
            status=PlanStatus.APPROVED,
        )
        journal = ExecutionJournal(plan.plan_id)
        journal.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T1,
        )
        journal.append(
            ExecutionEventType.STEP_APPROVED,
            T2,
            step_id="step.one",
        )
        checkpoint = ExecutionCheckpoint.capture(
            plan,
            journal,
            checkpoint_id="checkpoint:approved",
            created_at=T3,
        )
        command = command_for(
            plan,
            journal,
            checkpoint,
            approval_reference="approval:new",
        )

        with self.assertRaises(ResumeApplicationConflictError):
            ResumeApplication(command, checkpoint).apply(plan, journal)

    def test_timestamp_regression_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(
            plan,
            journal,
            checkpoint,
            issued_at="2026-08-03T23:59:59Z",
        )

        with self.assertRaises(JournalTimestampError):
            ResumeApplication(command, checkpoint).apply(plan, journal)


class ResultSerializationTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        original = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )

        restored = ResumeApplicationResult.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )

        data = json.loads(result.to_json())

        self.assertEqual(
            result.to_json(),
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ),
        )

    def test_tampered_result_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )
        data = result.to_dict()
        data["plan_after_hash"] = "1" * 64

        with self.assertRaises(ResumeApplicationIntegrityError):
            ResumeApplicationResult.from_dict(data)

    def test_tampered_updated_plan_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )
        data = result.to_dict()
        data["updated_plan"]["objective"] = "Tampered objective"

        with self.assertRaises(ResumeApplicationIntegrityError):
            ResumeApplicationResult.from_dict(data)

    def test_tampered_updated_event_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )
        data = result.to_dict()
        data["updated_events"][-1]["step_id"] = "step.other"

        with self.assertRaises(Exception):
            ResumeApplicationResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )
        data = result.to_dict()
        del data["result_hash"]

        with self.assertRaises(ResumeApplicationIntegrityError):
            ResumeApplicationResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )

        with self.assertRaises(FrozenInstanceError):
            result.status = ResumeApplicationStatus.ALREADY_APPLIED  # type: ignore[misc]

    def test_to_journal_returns_independent_journal(self) -> None:
        plan, journal, checkpoint = pending_context()
        command = command_for(plan, journal, checkpoint)
        result = ResumeApplication(command, checkpoint).apply(
            plan,
            journal,
        )

        first = result.to_journal()
        second = result.to_journal()

        self.assertIsNot(first, second)
        self.assertEqual(first.to_jsonl(), second.to_jsonl())


if __name__ == "__main__":
    unittest.main()
