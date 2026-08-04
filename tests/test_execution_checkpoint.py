from datetime import UTC, datetime, timedelta, timezone
from dataclasses import FrozenInstanceError
import json
import unittest

from elman_os.execution_checkpoint import (
    CheckpointIntegrityError,
    CheckpointStatus,
    ExecutionCheckpoint,
    ExecutionCheckpointError,
    PlanJournalCompatibilityError,
    ResumeAssessment,
    ResumeAssessmentStatus,
    StepCheckpointState,
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


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
T3 = "2026-08-04T00:00:03Z"
T4 = "2026-08-04T00:00:04Z"
T5 = "2026-08-04T00:00:05Z"
T6 = "2026-08-04T00:00:06Z"
T7 = "2026-08-04T00:00:07Z"
T8 = "2026-08-04T00:00:08Z"


def step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    assigned_agent_id: str | None = None,
    status: StepStatus = StepStatus.PENDING,
    title_suffix: str = "",
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        title=f"Title {step_id}{title_suffix}",
        capability_id="build",
        objective=f"Objective {step_id}",
        dependencies=dependencies,
        required_permissions=("build",),
        assigned_agent_id=assigned_agent_id,
        status=status,
    )


def plan(
    steps: tuple[ExecutionStep, ...],
    *,
    plan_id: str = "plan:001",
    project_id: str = "project:001",
    status: PlanStatus = PlanStatus.PENDING,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        project_id=project_id,
        objective="Build a deterministic application",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=status,
        requires_human_approval=False,
    )


def pending_state() -> tuple[ExecutionPlan, ExecutionJournal]:
    current_plan = plan((step("step.one"),))
    journal = ExecutionJournal(current_plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
        payload={"project_id": current_plan.project_id},
    )
    return current_plan, journal


def running_state() -> tuple[ExecutionPlan, ExecutionJournal]:
    root = step(
        "step.root",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.COMPLETED,
    )
    final = step(
        "step.final",
        dependencies=("step.root",),
        assigned_agent_id="ELMAN_WEB",
        status=StepStatus.RUNNING,
    )
    current_plan = plan(
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
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        T7,
        step_id="step.final",
        agent_id="ELMAN_WEB",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        T8,
        step_id="step.final",
        agent_id="ELMAN_WEB",
    )
    return current_plan, journal


def blocked_state() -> tuple[ExecutionPlan, ExecutionJournal]:
    blocked = step(
        "step.one",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.BLOCKED,
    )
    current_plan = plan(
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
        T6,
        agent_id="ELMAN_NEXUS",
    )
    return current_plan, journal


def completed_state() -> tuple[ExecutionPlan, ExecutionJournal]:
    completed = step(
        "step.one",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.COMPLETED,
    )
    current_plan = plan(
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
        T6,
        agent_id="ELMAN_NEXUS",
    )
    return current_plan, journal


def checkpoint(
    current_plan: ExecutionPlan,
    journal: ExecutionJournal,
    *,
    checkpoint_id: str = "checkpoint:001",
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint.capture(
        current_plan,
        journal,
        checkpoint_id=checkpoint_id,
        created_at="2026-08-04T00:01:00Z",
    )


class StepCheckpointStateTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        state = StepCheckpointState(
            step_id="step.one",
            status=StepStatus.RUNNING,
            assigned_agent_id="ELMAN_CORE",
            approval_reference="approval:001",
        )

        restored = StepCheckpointState.from_dict(state.to_dict())

        self.assertEqual(restored, state)

    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StepCheckpointState(
                step_id="step.one",
                status="unknown",  # type: ignore[arg-type]
                assigned_agent_id=None,
                approval_reference=None,
            )

    def test_empty_step_id_is_rejected(self) -> None:
        with self.assertRaises(ExecutionCheckpointError):
            StepCheckpointState(
                step_id="",
                status=StepStatus.PENDING,
                assigned_agent_id=None,
                approval_reference=None,
            )

    def test_state_is_frozen(self) -> None:
        state = StepCheckpointState(
            step_id="step.one",
            status=StepStatus.PENDING,
            assigned_agent_id=None,
            approval_reference=None,
        )

        with self.assertRaises(FrozenInstanceError):
            state.step_id = "step.two"  # type: ignore[misc]


class CheckpointCaptureTests(unittest.TestCase):
    def test_pending_checkpoint_is_ready(self) -> None:
        current_plan, journal = pending_state()

        captured = checkpoint(current_plan, journal)

        self.assertIs(captured.checkpoint_status, CheckpointStatus.READY)
        self.assertIs(captured.plan_status, PlanStatus.PENDING)

    def test_running_checkpoint_is_ready(self) -> None:
        current_plan, journal = running_state()

        captured = checkpoint(current_plan, journal)

        self.assertIs(captured.checkpoint_status, CheckpointStatus.READY)
        self.assertEqual(captured.journal_event_count, 9)

    def test_blocked_checkpoint_is_blocked(self) -> None:
        current_plan, journal = blocked_state()

        captured = checkpoint(current_plan, journal)

        self.assertIs(captured.checkpoint_status, CheckpointStatus.BLOCKED)

    def test_completed_checkpoint_is_terminal(self) -> None:
        current_plan, journal = completed_state()

        captured = checkpoint(current_plan, journal)

        self.assertIs(captured.checkpoint_status, CheckpointStatus.TERMINAL)

    def test_capture_preserves_step_state_order(self) -> None:
        current_plan, journal = running_state()

        captured = checkpoint(current_plan, journal)

        self.assertEqual(
            tuple(item.step_id for item in captured.step_states),
            ("step.final", "step.root"),
        )

    def test_capture_links_journal_seal(self) -> None:
        current_plan, journal = pending_state()
        seal = journal.seal()

        captured = checkpoint(current_plan, journal)

        self.assertEqual(captured.journal_event_count, seal.event_count)
        self.assertEqual(captured.journal_head_hash, seal.head_hash)
        self.assertEqual(captured.journal_hash, seal.journal_hash)

    def test_capture_computes_checkpoint_hash(self) -> None:
        current_plan, journal = pending_state()

        captured = checkpoint(current_plan, journal)

        self.assertEqual(len(captured.checkpoint_hash or ""), 64)
        self.assertEqual(captured.checkpoint_hash, captured.compute_hash())

    def test_capture_hash_is_deterministic(self) -> None:
        current_plan, journal = pending_state()

        first = checkpoint(current_plan, journal)
        second = checkpoint(current_plan, journal)

        self.assertEqual(first.checkpoint_hash, second.checkpoint_hash)

    def test_capture_accepts_utc_datetime(self) -> None:
        current_plan, journal = pending_state()

        captured = ExecutionCheckpoint.capture(
            current_plan,
            journal,
            checkpoint_id="checkpoint:001",
            created_at=datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
        )

        self.assertEqual(
            captured.created_at,
            "2026-08-04T00:01:00.000000Z",
        )

    def test_capture_rejects_naive_datetime(self) -> None:
        current_plan, journal = pending_state()

        with self.assertRaises(ExecutionCheckpointError):
            ExecutionCheckpoint.capture(
                current_plan,
                journal,
                checkpoint_id="checkpoint:001",
                created_at=datetime(2026, 8, 4, 0, 1),
            )

    def test_capture_rejects_non_utc_datetime(self) -> None:
        current_plan, journal = pending_state()

        with self.assertRaises(ExecutionCheckpointError):
            ExecutionCheckpoint.capture(
                current_plan,
                journal,
                checkpoint_id="checkpoint:001",
                created_at=datetime(
                    2026,
                    8,
                    4,
                    0,
                    1,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_capture_rejects_invalid_checkpoint_id(self) -> None:
        current_plan, journal = pending_state()

        with self.assertRaises(ExecutionCheckpointError):
            ExecutionCheckpoint.capture(
                current_plan,
                journal,
                checkpoint_id="bad id",
                created_at=T0,
            )

    def test_capture_rejects_plan_journal_id_mismatch(self) -> None:
        current_plan, _ = pending_state()
        foreign = ExecutionJournal("plan:other")
        foreign.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )

        with self.assertRaises(PlanJournalCompatibilityError):
            checkpoint(current_plan, foreign)

    def test_capture_rejects_plan_status_mismatch(self) -> None:
        current_plan, journal = pending_state()
        incompatible = plan(
            current_plan.steps,
            status=PlanStatus.APPROVED,
        )

        with self.assertRaises(PlanJournalCompatibilityError):
            checkpoint(incompatible, journal)

    def test_capture_rejects_step_status_mismatch(self) -> None:
        current_plan, journal = pending_state()
        changed = plan(
            (
                step(
                    "step.one",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.RUNNING,
                ),
            ),
            status=PlanStatus.RUNNING,
        )
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            agent_id="ELMAN_NEXUS",
        )

        with self.assertRaises(PlanJournalCompatibilityError):
            checkpoint(changed, journal)

    def test_capture_rejects_assigned_agent_mismatch(self) -> None:
        current_plan, journal = running_state()
        changed = plan(
            (
                step(
                    "step.root",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.COMPLETED,
                ),
                step(
                    "step.final",
                    dependencies=("step.root",),
                    assigned_agent_id="ELMAN_MOBILE",
                    status=StepStatus.RUNNING,
                ),
            ),
            status=PlanStatus.RUNNING,
        )

        with self.assertRaises(PlanJournalCompatibilityError):
            checkpoint(changed, journal)


class CheckpointSerializationTests(unittest.TestCase):
    def test_json_round_trip(self) -> None:
        current_plan, journal = running_state()
        original = checkpoint(current_plan, journal)

        restored = ExecutionCheckpoint.from_json(original.to_json())

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_json_is_canonical(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)

        data = json.loads(captured.to_json())

        self.assertEqual(
            captured.to_json(),
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ),
        )

    def test_tampered_plan_status_is_detected(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        data = captured.to_dict()
        data["plan_status"] = PlanStatus.APPROVED.value
        data["checkpoint_status"] = CheckpointStatus.READY.value

        with self.assertRaises(CheckpointIntegrityError):
            ExecutionCheckpoint.from_dict(data)

    def test_tampered_step_state_is_detected(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        data = captured.to_dict()
        data["step_states"][0]["status"] = StepStatus.FAILED.value

        with self.assertRaises(CheckpointIntegrityError):
            ExecutionCheckpoint.from_dict(data)

    def test_missing_checkpoint_hash_is_rejected(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        data = captured.to_dict()
        del data["checkpoint_hash"]

        with self.assertRaises(CheckpointIntegrityError):
            ExecutionCheckpoint.from_dict(data)

    def test_wrong_record_type_is_rejected(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        data = captured.to_dict()
        data["record_type"] = "other"

        with self.assertRaises(ExecutionCheckpointError):
            ExecutionCheckpoint.from_dict(data)

    def test_duplicate_step_states_are_rejected(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        data = captured.to_dict()
        data["step_states"].append(dict(data["step_states"][0]))
        data["checkpoint_hash"] = None

        with self.assertRaises(ExecutionCheckpointError):
            ExecutionCheckpoint(
                checkpoint_id=data["checkpoint_id"],
                created_at=data["created_at"],
                plan_id=data["plan_id"],
                project_id=data["project_id"],
                plan_status=PlanStatus(data["plan_status"]),
                checkpoint_status=CheckpointStatus(
                    data["checkpoint_status"]
                ),
                step_states=tuple(
                    StepCheckpointState.from_dict(item)
                    for item in data["step_states"]
                ),
                plan_definition_hash=data["plan_definition_hash"],
                plan_state_hash=data["plan_state_hash"],
                journal_event_count=data["journal_event_count"],
                journal_head_hash=data["journal_head_hash"],
                journal_hash=data["journal_hash"],
            )


class ResumeAssessmentTests(unittest.TestCase):
    def test_ready_pending_checkpoint_can_resume(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)

        assessment = captured.assess_resume(current_plan, journal)

        self.assertIs(assessment.status, ResumeAssessmentStatus.READY)
        self.assertTrue(assessment.can_resume)
        self.assertEqual(assessment.ready_step_ids, ("step.one",))

    def test_ready_running_checkpoint_can_resume(self) -> None:
        current_plan, journal = running_state()
        captured = checkpoint(current_plan, journal)

        assessment = captured.assess_resume(current_plan, journal)

        self.assertIs(assessment.status, ResumeAssessmentStatus.READY)
        self.assertTrue(assessment.can_resume)
        self.assertEqual(assessment.running_step_ids, ("step.final",))

    def test_blocked_checkpoint_cannot_resume(self) -> None:
        current_plan, journal = blocked_state()
        captured = checkpoint(current_plan, journal)

        assessment = captured.assess_resume(current_plan, journal)

        self.assertIs(assessment.status, ResumeAssessmentStatus.BLOCKED)
        self.assertFalse(assessment.can_resume)

    def test_terminal_checkpoint_cannot_resume(self) -> None:
        current_plan, journal = completed_state()
        captured = checkpoint(current_plan, journal)

        assessment = captured.assess_resume(current_plan, journal)

        self.assertIs(assessment.status, ResumeAssessmentStatus.TERMINAL)
        self.assertFalse(assessment.can_resume)

    def test_checkpoint_becomes_stale_after_journal_advances(self) -> None:
        old_plan, journal = pending_state()
        captured = checkpoint(old_plan, journal)

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
        current_plan = plan(
            (
                step(
                    "step.one",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.RUNNING,
                ),
            ),
            status=PlanStatus.RUNNING,
        )

        assessment = captured.assess_resume(current_plan, journal)

        self.assertIs(assessment.status, ResumeAssessmentStatus.STALE)
        self.assertFalse(assessment.can_resume)
        self.assertGreater(
            assessment.current_event_count,
            captured.journal_event_count,
        )

    def test_divergent_prefix_is_incompatible(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)

        divergent = ExecutionJournal(current_plan.plan_id)
        divergent.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
            payload={"different": True},
        )

        assessment = captured.assess_resume(current_plan, divergent)

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )
        self.assertFalse(assessment.can_resume)

    def test_shorter_journal_is_incompatible(self) -> None:
        current_plan, journal = running_state()
        captured = checkpoint(current_plan, journal)

        shorter = ExecutionJournal.from_events(
            journal.plan_id,
            journal.events[:5],
        )
        partial_plan = plan(
            (
                step(
                    "step.root",
                    assigned_agent_id="ELMAN_CORE",
                    status=StepStatus.RUNNING,
                ),
                step(
                    "step.final",
                    dependencies=("step.root",),
                ),
            ),
            status=PlanStatus.RUNNING,
        )

        assessment = captured.assess_resume(partial_plan, shorter)

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )

    def test_changed_definition_is_incompatible(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        changed_plan = plan(
            (
                step(
                    "step.one",
                    title_suffix=" changed",
                ),
            ),
        )

        assessment = captured.assess_resume(changed_plan, journal)

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )

    def test_changed_state_without_journal_is_incompatible(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        changed_plan = ExecutionPlan(
            plan_id=current_plan.plan_id,
            project_id=current_plan.project_id,
            objective=current_plan.objective,
            created_by=current_plan.created_by,
            steps=current_plan.steps,
            status=current_plan.status,
            requires_human_approval=current_plan.requires_human_approval,
            metadata={"changed": True},
        )

        assessment = captured.assess_resume(changed_plan, journal)

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )

    def test_project_mismatch_is_incompatible(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        foreign_plan = plan(
            current_plan.steps,
            project_id="project:other",
        )

        assessment = captured.assess_resume(foreign_plan, journal)

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )

    def test_plan_identifier_mismatch_is_incompatible(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)
        foreign_plan = plan(
            current_plan.steps,
            plan_id="plan:other",
        )
        foreign_journal = ExecutionJournal("plan:other")
        foreign_journal.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )

        assessment = captured.assess_resume(
            foreign_plan,
            foreign_journal,
        )

        self.assertIs(
            assessment.status,
            ResumeAssessmentStatus.INCOMPATIBLE,
        )

    def test_assessment_json_is_deterministic(self) -> None:
        current_plan, journal = pending_state()
        captured = checkpoint(current_plan, journal)

        first = captured.assess_resume(current_plan, journal)
        second = captured.assess_resume(current_plan, journal)

        self.assertEqual(first.to_json(), second.to_json())

    def test_assessment_is_frozen(self) -> None:
        assessment = ResumeAssessment(
            checkpoint_id="checkpoint:001",
            status=ResumeAssessmentStatus.READY,
            can_resume=True,
            reasons=("compatible",),
            ready_step_ids=("step.one",),
            running_step_ids=(),
            current_event_count=1,
            current_head_hash="0" * 64,
        )

        with self.assertRaises(FrozenInstanceError):
            assessment.can_resume = False  # type: ignore[misc]

    def test_non_ready_assessment_cannot_allow_resume(self) -> None:
        with self.assertRaises(ExecutionCheckpointError):
            ResumeAssessment(
                checkpoint_id="checkpoint:001",
                status=ResumeAssessmentStatus.STALE,
                can_resume=True,
                reasons=("stale",),
                ready_step_ids=(),
                running_step_ids=(),
                current_event_count=1,
                current_head_hash="0" * 64,
            )

    def test_ready_assessment_must_allow_resume(self) -> None:
        with self.assertRaises(ExecutionCheckpointError):
            ResumeAssessment(
                checkpoint_id="checkpoint:001",
                status=ResumeAssessmentStatus.READY,
                can_resume=False,
                reasons=("compatible",),
                ready_step_ids=(),
                running_step_ids=(),
                current_event_count=1,
                current_head_hash="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
