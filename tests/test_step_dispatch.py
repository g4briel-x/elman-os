from dataclasses import FrozenInstanceError, replace
import hashlib
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.agent_contracts import (
    AgentCapability,
    AgentDefinition,
    AgentRegistry,
)
from elman_os.execution_checkpoint import ExecutionCheckpoint
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
    ResumePolicy,
    ResumeRequest,
    decide_resume,
)
from elman_os.resume_application import (
    ResumeApplication,
    ResumeApplicationResult,
    ResumeApplicationStatus,
)
from elman_os.step_dispatch import (
    StepDispatch,
    StepDispatchConflictError,
    StepDispatchError,
    StepDispatchIntegrityError,
    StepDispatchRequest,
    StepDispatchResult,
    StepDispatchStatus,
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
T3 = "2026-08-04T00:00:03Z"
T4 = "2026-08-04T00:00:04Z"
T5 = "2026-08-04T00:00:05Z"
T6 = "2026-08-04T00:00:06Z"
RESUME_ISSUED = "2026-08-04T00:10:00Z"
DISPATCHED = "2026-08-04T00:20:00Z"


def make_agent(
    *,
    agent_id: str = "ELMAN_CORE",
    capability_id: str = "build",
    permissions: tuple[str, ...] = ("build",),
    capability_permissions: tuple[str, ...] = ("build",),
    requires_human_approval: bool = False,
    fail_closed: bool = True,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id,
        role="Build specialist",
        version="1.0.0",
        capabilities=(
            AgentCapability(
                capability_id=capability_id,
                description="Build one approved execution step",
                input_kinds=("json",),
                output_kinds=("json",),
                permissions=capability_permissions,
                requires_human_approval=requires_human_approval,
            ),
        ),
        permissions=permissions,
        fail_closed=fail_closed,
    )


def make_registry(
    definition: AgentDefinition | None = None,
) -> AgentRegistry:
    return AgentRegistry((definition or make_agent(),))


def make_step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    capability_id: str = "build",
    required_permissions: tuple[str, ...] = ("build",),
    assigned_agent_id: str | None = None,
    status: StepStatus = StepStatus.PENDING,
    requires_human_approval: bool = False,
    approval_reference: str | None = None,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        title=f"Title {step_id}",
        capability_id=capability_id,
        objective=f"Objective {step_id}",
        dependencies=dependencies,
        required_permissions=required_permissions,
        assigned_agent_id=assigned_agent_id,
        status=status,
        requires_human_approval=requires_human_approval,
        approval_reference=approval_reference,
        metadata={"source": "test"},
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
        approval_reference=approval_reference,
        requires_human_approval=requires_human_approval,
    )


def pending_resume_result(
    *,
    requested_step_ids: tuple[str, ...] = ("step.one",),
    step_one: ExecutionStep | None = None,
    step_two: ExecutionStep | None = None,
) -> ResumeApplicationResult:
    plan = make_plan(
        (
            step_one or make_step("step.one"),
            step_two or make_step("step.two"),
        )
    )
    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
    )
    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id="checkpoint:001",
        created_at=T1,
    )
    assessment = checkpoint.assess_resume(plan, journal)
    request = ResumeRequest(
        request_id="request:resume-001",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint.checkpoint_hash or "",
        plan_id=checkpoint.plan_id,
        requested_by="ELMAN_NEXUS",
        approval_reference="approval:resume-001",
        created_at=T2,
        rationale="Human operator approved controlled resume",
        requested_step_ids=requested_step_ids,
    )
    decision = decide_resume(
        request,
        checkpoint,
        assessment,
        ResumePolicy(policy_id="policy:resume-001"),
        issued_at=RESUME_ISSUED,
    )
    assert decision.command is not None
    return ResumeApplication(
        decision.command,
        checkpoint,
    ).apply(plan, journal)


def running_resume_result() -> ResumeApplicationResult:
    root = make_step(
        "step.root",
        assigned_agent_id="ELMAN_CORE",
        status=StepStatus.COMPLETED,
    )
    final = make_step(
        "step.final",
        dependencies=("step.root",),
    )
    plan = make_plan(
        (root, final),
        status=PlanStatus.RUNNING,
    )
    journal = ExecutionJournal(plan.plan_id)
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
        plan,
        journal,
        checkpoint_id="checkpoint:running",
        created_at="2026-08-04T00:01:00Z",
    )
    assessment = checkpoint.assess_resume(plan, journal)
    request = ResumeRequest(
        request_id="request:resume-running",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint.checkpoint_hash or "",
        plan_id=checkpoint.plan_id,
        requested_by="ELMAN_NEXUS",
        approval_reference="approval:resume-running",
        created_at="2026-08-04T00:08:00Z",
        rationale="Authorize final step",
        requested_step_ids=("step.final",),
    )
    decision = decide_resume(
        request,
        checkpoint,
        assessment,
        ResumePolicy(policy_id="policy:resume-running"),
        issued_at=RESUME_ISSUED,
    )
    assert decision.command is not None
    return ResumeApplication(
        decision.command,
        checkpoint,
    ).apply(plan, journal)


def make_dispatch_request(
    result: ResumeApplicationResult,
    *,
    step_id: str = "step.one",
    agent_id: str = "ELMAN_CORE",
    requested_by: str = "ELMAN_NEXUS",
    created_at: str | datetime = DISPATCHED,
    dispatch_id: str | None = None,
) -> StepDispatchRequest:
    return StepDispatchRequest.from_resume_application(
        result,
        step_id=step_id,
        agent_id=agent_id,
        requested_by=requested_by,
        created_at=created_at,
        dispatch_id=dispatch_id,
    )


class StepDispatchRequestTests(unittest.TestCase):
    def test_request_is_derived_from_resume_boundary(self) -> None:
        result = pending_resume_result()
        request = make_dispatch_request(result)
        journal = result.to_journal()
        seal = journal.seal()

        self.assertEqual(request.plan_id, result.plan_id)
        self.assertEqual(
            request.resume_application_result_hash,
            result.result_hash,
        )
        self.assertEqual(
            request.journal_event_count,
            seal.event_count,
        )
        self.assertEqual(request.journal_head_hash, seal.head_hash)

    def test_default_dispatch_id_is_deterministic(self) -> None:
        result = pending_resume_result()

        first = make_dispatch_request(result)
        second = make_dispatch_request(result)

        self.assertEqual(first.dispatch_id, second.dispatch_id)
        self.assertEqual(first.request_hash, second.request_hash)

    def test_request_accepts_explicit_dispatch_id(self) -> None:
        result = pending_resume_result()

        request = make_dispatch_request(
            result,
            dispatch_id="dispatch:operator-001",
        )

        self.assertEqual(request.dispatch_id, "dispatch:operator-001")

    def test_request_json_round_trip(self) -> None:
        original = make_dispatch_request(pending_resume_result())

        restored = StepDispatchRequest.from_json(original.to_json())

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        request = make_dispatch_request(pending_resume_result())
        data = request.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(StepDispatchIntegrityError):
            StepDispatchRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        result = pending_resume_result()

        with self.assertRaises(StepDispatchError):
            make_dispatch_request(
                result,
                created_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_accepts_utc_datetime(self) -> None:
        result = pending_resume_result()

        request = make_dispatch_request(
            result,
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(
            request.created_at,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_request_is_frozen(self) -> None:
        request = make_dispatch_request(pending_resume_result())

        with self.assertRaises(FrozenInstanceError):
            request.step_id = "step.other"  # type: ignore[misc]


class StepDispatchConstructionTests(unittest.TestCase):
    def test_dispatch_accepts_matching_inputs(self) -> None:
        result = pending_resume_result()
        dispatch = StepDispatch(
            make_dispatch_request(result),
            result,
            make_registry(),
        )

        self.assertEqual(dispatch.request.step_id, "step.one")

    def test_dispatch_rejects_resume_result_hash_mismatch(self) -> None:
        first = pending_resume_result()
        second = pending_resume_result(
            requested_step_ids=("step.two",),
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(first),
                second,
                make_registry(),
            )

    def test_dispatch_rejects_step_not_authorized_by_resume(self) -> None:
        result = pending_resume_result(
            requested_step_ids=("step.one",),
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(result, step_id="step.two"),
                result,
                make_registry(),
            )

    def test_dispatch_is_frozen(self) -> None:
        result = pending_resume_result()
        dispatch = StepDispatch(
            make_dispatch_request(result),
            result,
            make_registry(),
        )

        with self.assertRaises(FrozenInstanceError):
            dispatch.registry = make_registry()  # type: ignore[misc]


class StepDispatchPreparationTests(unittest.TestCase):
    def test_prepare_transitions_plan_and_step_to_running(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        self.assertIs(result.status, StepDispatchStatus.PREPARED)
        self.assertIs(result.updated_plan.status, PlanStatus.RUNNING)
        step = result.updated_plan.get_step("step.one")
        self.assertIs(step.status, StepStatus.RUNNING)
        self.assertEqual(step.assigned_agent_id, "ELMAN_CORE")

    def test_prepare_preserves_other_step(self) -> None:
        resume = pending_resume_result(
            requested_step_ids=("step.one", "step.two"),
        )
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        other = result.updated_plan.get_step("step.two")
        self.assertIs(other.status, StepStatus.APPROVED)
        self.assertIsNone(other.assigned_agent_id)

    def test_prepare_appends_plan_assigned_and_started_events(self) -> None:
        resume = pending_resume_result()
        journal = resume.to_journal()
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, journal)
        appended = result.updated_events[journal.event_count:]

        self.assertEqual(
            tuple(event.event_type for event in appended),
            (
                ExecutionEventType.PLAN_STARTED,
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
            ),
        )

    def test_running_plan_appends_only_step_events(self) -> None:
        resume = running_resume_result()
        journal = resume.to_journal()
        request = make_dispatch_request(
            resume,
            step_id="step.final",
        )

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, journal)
        appended = result.updated_events[journal.event_count:]

        self.assertEqual(
            tuple(event.event_type for event in appended),
            (
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
            ),
        )

    def test_prepare_builds_strict_agent_request(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
        agent_request = result.agent_request

        self.assertEqual(
            agent_request.request_id,
            f"agent-request:{request.request_hash}",
        )
        self.assertEqual(agent_request.project_id, "project:001")
        self.assertEqual(agent_request.capability_id, "build")
        self.assertEqual(
            agent_request.inputs["resume_command_hash"],
            resume.command_hash,
        )
        self.assertFalse(
            agent_request.constraints["provider_call_allowed"]
        )
        self.assertFalse(
            agent_request.constraints["project_write_allowed"]
        )

    def test_prepare_links_journal_markers(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
        marker = result.updated_events[-1].payload

        self.assertEqual(
            marker["step_dispatch_id"],
            request.dispatch_id,
        )
        self.assertEqual(
            marker["step_dispatch_request_hash"],
            request.request_hash,
        )
        self.assertEqual(
            marker["resume_application_result_hash"],
            resume.result_hash,
        )

    def test_prepare_preserves_plan_journal_compatibility(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)

        result = StepDispatch(
            request,
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        checkpoint = ExecutionCheckpoint.capture(
            result.updated_plan,
            result.to_journal(),
            checkpoint_id="checkpoint:after-dispatch",
            created_at=DISPATCHED,
        )
        self.assertEqual(checkpoint.plan_id, result.plan_id)

    def test_prepare_does_not_mutate_inputs(self) -> None:
        resume = pending_resume_result()
        plan = resume.updated_plan
        journal = resume.to_journal()
        plan_json = plan.to_json()
        journal_jsonl = journal.to_jsonl()

        StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(plan, journal)

        self.assertEqual(plan.to_json(), plan_json)
        self.assertEqual(journal.to_jsonl(), journal_jsonl)

    def test_prepare_is_deterministic(self) -> None:
        resume = pending_resume_result()
        dispatch = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        )

        first = dispatch.prepare(
            resume.updated_plan,
            resume.to_journal(),
        )
        second = dispatch.prepare(
            resume.updated_plan,
            resume.to_journal(),
        )

        self.assertEqual(first.to_json(), second.to_json())


class StepDispatchValidationTests(unittest.TestCase):
    def test_unknown_agent_is_rejected(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(
            resume,
            agent_id="ELMAN_UNKNOWN",
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                request,
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, resume.to_journal())

    def test_agent_without_capability_is_rejected(self) -> None:
        resume = pending_resume_result()
        registry = make_registry(
            make_agent(capability_id="review"),
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                registry,
            ).prepare(resume.updated_plan, resume.to_journal())

    def test_agent_without_permission_is_rejected(self) -> None:
        resume = pending_resume_result()
        registry = make_registry(
            make_agent(
                permissions=(),
                capability_permissions=(),
            ),
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                registry,
            ).prepare(resume.updated_plan, resume.to_journal())

    def test_non_fail_closed_agent_is_rejected(self) -> None:
        resume = pending_resume_result()
        registry = make_registry(
            make_agent(fail_closed=False),
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                registry,
            ).prepare(resume.updated_plan, resume.to_journal())

    def test_incomplete_dependencies_are_rejected(self) -> None:
        pending_dependency = make_step("step.two")
        approved_dependent = make_step(
            "step.one",
            dependencies=("step.two",),
            status=StepStatus.APPROVED,
        )
        plan = make_plan(
            (approved_dependent, pending_dependency),
            status=PlanStatus.APPROVED,
            approval_reference="approval:resume-001",
        )
        before_journal = ExecutionJournal(plan.plan_id)
        before_journal.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )
        before_seal = before_journal.seal()

        journal = ExecutionJournal.from_events(
            plan.plan_id,
            before_journal.events,
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            RESUME_ISSUED,
        )
        journal.append(
            ExecutionEventType.STEP_APPROVED,
            RESUME_ISSUED,
            step_id="step.one",
        )
        after_seal = journal.seal()
        command_hash = "a" * 64
        resume = ResumeApplicationResult(
            application_id=f"application:{command_hash}",
            status=ResumeApplicationStatus.APPLIED,
            command_id="command:manual-resume",
            command_hash=command_hash,
            checkpoint_id="checkpoint:manual-resume",
            checkpoint_hash="b" * 64,
            plan_id=plan.plan_id,
            selected_step_ids=("step.one",),
            appended_event_sequences=(2, 3),
            plan_before_hash=hashlib.sha256(
                make_plan(
                    (
                        make_step(
                            "step.one",
                            dependencies=("step.two",),
                        ),
                        pending_dependency,
                    )
                ).to_json().encode("utf-8")
            ).hexdigest(),
            plan_after_hash=hashlib.sha256(
                plan.to_json().encode("utf-8")
            ).hexdigest(),
            journal_before_event_count=before_seal.event_count,
            journal_after_event_count=after_seal.event_count,
            journal_before_head_hash=before_seal.head_hash,
            journal_after_head_hash=after_seal.head_hash,
            journal_before_hash=before_seal.journal_hash,
            journal_after_hash=after_seal.journal_hash,
            applied_at=RESUME_ISSUED,
            updated_plan=plan,
            updated_events=journal.events,
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, resume.to_journal())

    def test_conflicting_agent_assignment_is_rejected(self) -> None:
        resume = pending_resume_result()
        conflicting_step = replace(
            resume.updated_plan.get_step("step.one"),
            assigned_agent_id="ELMAN_OTHER",
        )
        plan = replace(
            resume.updated_plan,
            steps=(
                conflicting_step,
                resume.updated_plan.get_step("step.two"),
            ),
        )
        journal = resume.to_journal()
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            "2026-08-04T00:15:00Z",
            step_id="step.one",
            agent_id="ELMAN_OTHER",
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                make_registry(),
            ).prepare(plan, journal)

    def test_changed_plan_boundary_is_rejected(self) -> None:
        resume = pending_resume_result()
        changed = replace(
            resume.updated_plan,
            objective="Changed objective",
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                make_registry(),
            ).prepare(changed, resume.to_journal())

    def test_advanced_journal_boundary_is_rejected(self) -> None:
        resume = pending_resume_result()
        journal = resume.to_journal()
        journal.append(
            ExecutionEventType.STEP_READY,
            "2026-08-04T00:15:00Z",
            step_id="step.one",
        )

        with self.assertRaises(StepDispatchError):
            StepDispatch(
                make_dispatch_request(resume),
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, journal)

    def test_timestamp_regression_is_rejected(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(
            resume,
            created_at="2026-08-03T23:59:59Z",
        )

        with self.assertRaises(JournalTimestampError):
            StepDispatch(
                request,
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, resume.to_journal())


class StepDispatchIdempotencyTests(unittest.TestCase):
    def test_second_prepare_is_already_prepared(self) -> None:
        resume = pending_resume_result()
        dispatch = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        )
        first = dispatch.prepare(
            resume.updated_plan,
            resume.to_journal(),
        )

        second = dispatch.prepare(
            first.updated_plan,
            first.to_journal(),
        )

        self.assertIs(
            second.status,
            StepDispatchStatus.ALREADY_PREPARED,
        )
        self.assertEqual(second.appended_event_sequences, ())

    def test_already_prepared_does_not_change_state(self) -> None:
        resume = pending_resume_result()
        dispatch = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        )
        first = dispatch.prepare(
            resume.updated_plan,
            resume.to_journal(),
        )
        current_journal = first.to_journal()

        second = dispatch.prepare(
            first.updated_plan,
            current_journal,
        )

        self.assertEqual(
            second.updated_plan.to_json(),
            first.updated_plan.to_json(),
        )
        self.assertEqual(
            second.to_journal().to_jsonl(),
            current_journal.to_jsonl(),
        )

    def test_partial_dispatch_markers_are_rejected(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)
        journal = resume.to_journal()
        marker = {
            "step_dispatch_id": request.dispatch_id,
            "step_dispatch_request_hash": request.request_hash,
            "resume_application_id": request.resume_application_id,
            "resume_application_result_hash": (
                request.resume_application_result_hash
            ),
            "resume_command_id": request.command_id,
            "resume_command_hash": request.command_hash,
            "step_dispatch_step_id": request.step_id,
            "step_dispatch_agent_id": request.agent_id,
        }
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            DISPATCHED,
            agent_id=request.requested_by,
            payload=marker,
        )

        with self.assertRaises(StepDispatchIntegrityError):
            StepDispatch(
                request,
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, journal)

    def test_same_dispatch_id_with_other_hash_is_rejected(self) -> None:
        resume = pending_resume_result()
        request = make_dispatch_request(resume)
        journal = resume.to_journal()
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            DISPATCHED,
            agent_id=request.requested_by,
            payload={
                "step_dispatch_id": request.dispatch_id,
                "step_dispatch_request_hash": "1" * 64,
            },
        )

        with self.assertRaises(StepDispatchConflictError):
            StepDispatch(
                request,
                resume,
                make_registry(),
            ).prepare(resume.updated_plan, journal)


class StepDispatchResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        restored = StepDispatchResult.from_json(result.to_json())

        self.assertEqual(restored, result)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
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
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
        data = result.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(StepDispatchIntegrityError):
            StepDispatchResult.from_dict(data)

    def test_tampered_updated_plan_is_rejected(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
        data = result.to_dict()
        data["updated_plan"]["objective"] = "Tampered"

        with self.assertRaises(StepDispatchIntegrityError):
            StepDispatchResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())
        data = result.to_dict()
        del data["result_hash"]

        with self.assertRaises(StepDispatchIntegrityError):
            StepDispatchResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        with self.assertRaises(FrozenInstanceError):
            result.status = StepDispatchStatus.ALREADY_PREPARED  # type: ignore[misc]

    def test_to_journal_returns_independent_journal(self) -> None:
        resume = pending_resume_result()
        result = StepDispatch(
            make_dispatch_request(resume),
            resume,
            make_registry(),
        ).prepare(resume.updated_plan, resume.to_journal())

        first = result.to_journal()
        second = result.to_journal()

        self.assertIsNot(first, second)
        self.assertEqual(first.to_jsonl(), second.to_jsonl())


if __name__ == "__main__":
    unittest.main()
