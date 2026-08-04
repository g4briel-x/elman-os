from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.agent_contracts import (
    AgentCapability,
    AgentDefinition,
    AgentRegistry,
    AgentResponse,
    AgentResponseStatus,
)
from elman_os.agent_response_ingestion import (
    AgentResponseIngestion,
    AgentResponseIngestionConflictError,
    AgentResponseIngestionError,
    AgentResponseIngestionIntegrityError,
    AgentResponseIngestionRequest,
    AgentResponseIngestionResult,
    AgentResponseIngestionStatus,
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
from elman_os.resume_application import ResumeApplication
from elman_os.step_dispatch import (
    StepDispatch,
    StepDispatchRequest,
    StepDispatchResult,
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
RESUME_ISSUED = "2026-08-04T00:10:00Z"
DISPATCHED = "2026-08-04T00:20:00Z"
RECEIVED = "2026-08-04T00:30:00Z"


def make_registry() -> AgentRegistry:
    return AgentRegistry(
        (
            AgentDefinition(
                agent_id="ELMAN_CORE",
                name="ELMAN Core",
                role="Build specialist",
                version="1.0.0",
                capabilities=(
                    AgentCapability(
                        capability_id="build",
                        description="Build one execution step",
                        input_kinds=("json",),
                        output_kinds=("json",),
                        permissions=("build",),
                    ),
                ),
                permissions=("build",),
                fail_closed=True,
            ),
        )
    )


def make_step(
    step_id: str,
    *,
    status: StepStatus = StepStatus.PENDING,
    assigned_agent_id: str | None = None,
    dependencies: tuple[str, ...] = (),
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
        metadata={"source": "test"},
    )


def make_dispatch_result(
    *,
    two_steps: bool = False,
) -> StepDispatchResult:
    steps = (
        (make_step("step.one"), make_step("step.two"))
        if two_steps
        else (make_step("step.one"),)
    )
    plan = ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Build safely",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=PlanStatus.PENDING,
        requires_human_approval=False,
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
    resume_request = ResumeRequest(
        request_id="request:resume-001",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint.checkpoint_hash or "",
        plan_id=checkpoint.plan_id,
        requested_by="ELMAN_NEXUS",
        approval_reference="approval:resume-001",
        created_at=T2,
        rationale="Human operator approved controlled resume",
        requested_step_ids=("step.one",),
    )
    decision = decide_resume(
        resume_request,
        checkpoint,
        assessment,
        ResumePolicy(policy_id="policy:resume-001"),
        issued_at=RESUME_ISSUED,
    )
    assert decision.command is not None
    resume_result = ResumeApplication(
        decision.command,
        checkpoint,
    ).apply(plan, journal)

    dispatch_request = StepDispatchRequest.from_resume_application(
        resume_result,
        step_id="step.one",
        agent_id="ELMAN_CORE",
        requested_by="ELMAN_NEXUS",
        created_at=DISPATCHED,
    )
    return StepDispatch(
        dispatch_request,
        resume_result,
        make_registry(),
    ).prepare(
        resume_result.updated_plan,
        resume_result.to_journal(),
    )


def make_response(
    dispatch: StepDispatchResult,
    status: AgentResponseStatus = AgentResponseStatus.SUCCEEDED,
) -> AgentResponse:
    if status is AgentResponseStatus.SUCCEEDED:
        return AgentResponse(
            request_id=dispatch.agent_request.request_id,
            agent_id=dispatch.agent_id,
            status=status,
            summary="Step completed successfully",
            outputs={
                "artifact": {
                    "path": "generated/output.txt",
                    "sha256": "a" * 64,
                }
            },
            evidence=("unit-tests-pass",),
            confidence=0.97,
        )
    if status is AgentResponseStatus.BLOCKED:
        return AgentResponse(
            request_id=dispatch.agent_request.request_id,
            agent_id=dispatch.agent_id,
            status=status,
            summary="Step is blocked",
            warnings=("human input required",),
            confidence=0.55,
        )
    return AgentResponse(
        request_id=dispatch.agent_request.request_id,
        agent_id=dispatch.agent_id,
        status=status,
        summary="Step failed",
        errors=("provider response was invalid",),
        confidence=0.2,
    )


def make_request(
    dispatch: StepDispatchResult,
    response: AgentResponse,
    *,
    received_at: str | datetime = RECEIVED,
    ingestion_id: str | None = None,
) -> AgentResponseIngestionRequest:
    return AgentResponseIngestionRequest.from_dispatch_result(
        dispatch,
        response,
        received_at=received_at,
        ingestion_id=ingestion_id,
    )


class IngestionRequestTests(unittest.TestCase):
    def test_request_is_derived_from_dispatch_boundary(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(dispatch, response)
        seal = dispatch.to_journal().seal()

        self.assertEqual(request.dispatch_id, dispatch.dispatch_id)
        self.assertEqual(request.step_id, dispatch.step_id)
        self.assertEqual(
            request.agent_request_id,
            dispatch.agent_request.request_id,
        )
        self.assertEqual(request.journal_event_count, seal.event_count)
        self.assertEqual(request.journal_head_hash, seal.head_hash)

    def test_default_ingestion_id_is_deterministic(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)

        first = make_request(dispatch, response)
        second = make_request(dispatch, response)

        self.assertEqual(first.ingestion_id, second.ingestion_id)
        self.assertEqual(first.request_hash, second.request_hash)

    def test_request_accepts_explicit_ingestion_id(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)

        request = make_request(
            dispatch,
            response,
            ingestion_id="ingestion:operator-001",
        )

        self.assertEqual(
            request.ingestion_id,
            "ingestion:operator-001",
        )

    def test_request_json_round_trip(self) -> None:
        dispatch = make_dispatch_result()
        original = make_request(
            dispatch,
            make_response(dispatch),
        )

        restored = AgentResponseIngestionRequest.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        dispatch = make_dispatch_result()
        request = make_request(
            dispatch,
            make_response(dispatch),
        )
        data = request.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            AgentResponseIngestionRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        dispatch = make_dispatch_result()

        with self.assertRaises(AgentResponseIngestionError):
            make_request(
                dispatch,
                make_response(dispatch),
                received_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_accepts_utc_datetime(self) -> None:
        dispatch = make_dispatch_result()

        request = make_request(
            dispatch,
            make_response(dispatch),
            received_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(
            request.received_at,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_request_is_frozen(self) -> None:
        dispatch = make_dispatch_result()
        request = make_request(
            dispatch,
            make_response(dispatch),
        )

        with self.assertRaises(FrozenInstanceError):
            request.step_id = "step.other"  # type: ignore[misc]


class IngestionConstructionTests(unittest.TestCase):
    def test_ingestion_accepts_matching_inputs(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )

        self.assertEqual(
            ingestion.request.agent_request_id,
            dispatch.agent_request.request_id,
        )

    def test_ingestion_rejects_other_dispatch_result(self) -> None:
        first = make_dispatch_result()
        second = make_dispatch_result(two_steps=True)
        response = make_response(first)

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(first, response),
                second,
                response,
            )

    def test_request_creation_rejects_response_request_mismatch(self) -> None:
        dispatch = make_dispatch_result()
        response = AgentResponse(
            request_id="agent-request:other",
            agent_id=dispatch.agent_id,
            status=AgentResponseStatus.SUCCEEDED,
            summary="Done",
        )

        with self.assertRaises(AgentResponseIngestionError):
            make_request(dispatch, response)

    def test_request_creation_rejects_response_agent_mismatch(self) -> None:
        dispatch = make_dispatch_result()
        response = AgentResponse(
            request_id=dispatch.agent_request.request_id,
            agent_id="ELMAN_OTHER",
            status=AgentResponseStatus.SUCCEEDED,
            summary="Done",
        )

        with self.assertRaises(AgentResponseIngestionError):
            make_request(dispatch, response)

    def test_ingestion_rejects_changed_response_status(self) -> None:
        dispatch = make_dispatch_result()
        succeeded = make_response(dispatch)
        request = make_request(dispatch, succeeded)
        blocked = make_response(
            dispatch,
            AgentResponseStatus.BLOCKED,
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(request, dispatch, blocked)

    def test_ingestion_is_frozen(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )

        with self.assertRaises(FrozenInstanceError):
            ingestion.response = response  # type: ignore[misc]


class SuccessfulIngestionTests(unittest.TestCase):
    def test_single_step_success_completes_plan(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        self.assertIs(
            result.status,
            AgentResponseIngestionStatus.INGESTED,
        )
        self.assertIs(result.updated_plan.status, PlanStatus.COMPLETED)
        self.assertIs(
            result.updated_plan.get_step("step.one").status,
            StepStatus.COMPLETED,
        )

    def test_single_step_success_appends_step_and_plan_events(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        journal = dispatch.to_journal()

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(dispatch.updated_plan, journal)
        appended = result.updated_events[journal.event_count:]

        self.assertEqual(
            tuple(event.event_type for event in appended),
            (
                ExecutionEventType.STEP_COMPLETED,
                ExecutionEventType.PLAN_COMPLETED,
            ),
        )

    def test_multi_step_success_keeps_plan_running(self) -> None:
        dispatch = make_dispatch_result(two_steps=True)
        response = make_response(dispatch)
        journal = dispatch.to_journal()

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(dispatch.updated_plan, journal)

        self.assertIs(result.updated_plan.status, PlanStatus.RUNNING)
        self.assertIs(
            result.updated_plan.get_step("step.one").status,
            StepStatus.COMPLETED,
        )
        self.assertIs(
            result.updated_plan.get_step("step.two").status,
            StepStatus.PENDING,
        )
        self.assertEqual(
            tuple(
                event.event_type
                for event in result.updated_events[journal.event_count:]
            ),
            (ExecutionEventType.STEP_COMPLETED,),
        )

    def test_response_outputs_are_preserved_but_not_applied(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        self.assertEqual(
            result.response.outputs["artifact"]["path"],
            "generated/output.txt",
        )
        self.assertNotIn(
            "generated/output.txt",
            result.updated_plan.to_json(),
        )

    def test_journal_markers_link_response_and_dispatch(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(dispatch, response)

        result = AgentResponseIngestion(
            request,
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        marker = result.updated_events[-1].payload

        self.assertEqual(
            marker["agent_response_ingestion_id"],
            request.ingestion_id,
        )
        self.assertEqual(
            marker["agent_response_hash"],
            request.response_hash,
        )
        self.assertEqual(
            marker["step_dispatch_result_hash"],
            dispatch.result_hash,
        )

    def test_result_preserves_plan_journal_compatibility(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        checkpoint = ExecutionCheckpoint.capture(
            result.updated_plan,
            result.to_journal(),
            checkpoint_id="checkpoint:after-ingestion",
            created_at=RECEIVED,
        )
        self.assertEqual(checkpoint.plan_id, result.plan_id)

    def test_ingestion_does_not_mutate_inputs(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        plan = dispatch.updated_plan
        journal = dispatch.to_journal()
        plan_json = plan.to_json()
        journal_jsonl = journal.to_jsonl()

        AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(plan, journal)

        self.assertEqual(plan.to_json(), plan_json)
        self.assertEqual(journal.to_jsonl(), journal_jsonl)

    def test_ingestion_is_deterministic(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )

        first = ingestion.ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        second = ingestion.ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        self.assertEqual(first.to_json(), second.to_json())


class BlockedAndFailedIngestionTests(unittest.TestCase):
    def test_blocked_response_blocks_step_and_plan(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(
            dispatch,
            AgentResponseStatus.BLOCKED,
        )
        journal = dispatch.to_journal()

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(dispatch.updated_plan, journal)

        self.assertIs(result.updated_plan.status, PlanStatus.BLOCKED)
        self.assertIs(
            result.updated_plan.get_step("step.one").status,
            StepStatus.BLOCKED,
        )
        self.assertEqual(
            tuple(
                event.event_type
                for event in result.updated_events[journal.event_count:]
            ),
            (
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.PLAN_BLOCKED,
            ),
        )
        self.assertEqual(
            result.response.warnings,
            ("human input required",),
        )

    def test_failed_response_fails_step_and_plan(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(
            dispatch,
            AgentResponseStatus.FAILED,
        )
        journal = dispatch.to_journal()

        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(dispatch.updated_plan, journal)

        self.assertIs(result.updated_plan.status, PlanStatus.FAILED)
        self.assertIs(
            result.updated_plan.get_step("step.one").status,
            StepStatus.FAILED,
        )
        self.assertEqual(
            tuple(
                event.event_type
                for event in result.updated_events[journal.event_count:]
            ),
            (
                ExecutionEventType.STEP_FAILED,
                ExecutionEventType.PLAN_FAILED,
            ),
        )
        self.assertEqual(
            result.response.errors,
            ("provider response was invalid",),
        )


class IngestionValidationTests(unittest.TestCase):
    def test_plan_identifier_mismatch_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        foreign = replace(
            dispatch.updated_plan,
            plan_id="plan:other",
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(dispatch, response),
                dispatch,
                response,
            ).ingest(foreign, dispatch.to_journal())

    def test_journal_identifier_mismatch_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        foreign = ExecutionJournal("plan:other")
        foreign.append(
            ExecutionEventType.PLAN_CREATED,
            T0,
            agent_id="ELMAN_NEXUS",
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(dispatch, response),
                dispatch,
                response,
            ).ingest(dispatch.updated_plan, foreign)

    def test_changed_plan_boundary_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        changed = replace(
            dispatch.updated_plan,
            objective="Changed objective",
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(dispatch, response),
                dispatch,
                response,
            ).ingest(changed, dispatch.to_journal())

    def test_advanced_journal_boundary_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        journal = dispatch.to_journal()
        journal.append(
            ExecutionEventType.STEP_READY,
            "2026-08-04T00:25:00Z",
            step_id="step.one",
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(dispatch, response),
                dispatch,
                response,
            ).ingest(dispatch.updated_plan, journal)

    def test_conflicting_agent_assignment_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        running = dispatch.updated_plan.get_step("step.one")
        conflicting = replace(
            running,
            assigned_agent_id="ELMAN_OTHER",
        )
        changed = replace(
            dispatch.updated_plan,
            steps=(conflicting,),
        )

        with self.assertRaises(AgentResponseIngestionError):
            AgentResponseIngestion(
                make_request(dispatch, response),
                dispatch,
                response,
            ).ingest(changed, dispatch.to_journal())

    def test_timestamp_regression_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(
            dispatch,
            response,
            received_at="2026-08-03T23:59:59Z",
        )

        with self.assertRaises(JournalTimestampError):
            AgentResponseIngestion(
                request,
                dispatch,
                response,
            ).ingest(
                dispatch.updated_plan,
                dispatch.to_journal(),
            )


class IngestionIdempotencyTests(unittest.TestCase):
    def test_second_ingestion_is_already_ingested(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )
        first = ingestion.ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        second = ingestion.ingest(
            first.updated_plan,
            first.to_journal(),
        )

        self.assertIs(
            second.status,
            AgentResponseIngestionStatus.ALREADY_INGESTED,
        )
        self.assertEqual(second.appended_event_sequences, ())

    def test_already_ingested_does_not_change_state(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )
        first = ingestion.ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        current_journal = first.to_journal()

        second = ingestion.ingest(
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

    def test_partial_ingestion_markers_are_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(dispatch, response)
        journal = dispatch.to_journal()
        marker = {
            "agent_response_ingestion_id": request.ingestion_id,
            "agent_response_ingestion_request_hash": (
                request.request_hash
            ),
            "agent_response_hash": request.response_hash,
            "agent_response_request_id": request.agent_request_id,
            "agent_response_status": request.response_status.value,
            "step_dispatch_id": request.dispatch_id,
            "step_dispatch_result_hash": (
                request.dispatch_result_hash
            ),
            "agent_response_step_id": request.step_id,
            "agent_response_agent_id": request.agent_id,
        }
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            RECEIVED,
            step_id=request.step_id,
            agent_id=request.agent_id,
            payload=marker,
        )

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            AgentResponseIngestion(
                request,
                dispatch,
                response,
            ).ingest(dispatch.updated_plan, journal)

    def test_same_ingestion_id_with_other_hash_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(dispatch, response)
        journal = dispatch.to_journal()
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            RECEIVED,
            step_id=request.step_id,
            agent_id=request.agent_id,
            payload={
                "agent_response_ingestion_id": (
                    request.ingestion_id
                ),
                "agent_response_ingestion_request_hash": "1" * 64,
            },
        )

        with self.assertRaises(
            AgentResponseIngestionConflictError
        ):
            AgentResponseIngestion(
                request,
                dispatch,
                response,
            ).ingest(dispatch.updated_plan, journal)

    def test_same_agent_request_with_other_response_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        request = make_request(dispatch, response)
        journal = dispatch.to_journal()
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            RECEIVED,
            step_id=request.step_id,
            agent_id=request.agent_id,
            payload={
                "agent_response_request_id": (
                    request.agent_request_id
                ),
                "agent_response_hash": "1" * 64,
            },
        )

        with self.assertRaises(
            AgentResponseIngestionConflictError
        ):
            AgentResponseIngestion(
                request,
                dispatch,
                response,
            ).ingest(dispatch.updated_plan, journal)

    def test_replay_after_later_journal_progress_is_rejected(self) -> None:
        dispatch = make_dispatch_result(two_steps=True)
        response = make_response(dispatch)
        ingestion = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        )
        first = ingestion.ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        journal = first.to_journal()
        journal.append(
            ExecutionEventType.STEP_READY,
            "2026-08-04T00:31:00Z",
            step_id="step.two",
        )

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            ingestion.ingest(first.updated_plan, journal)


class IngestionResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        original = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        restored = AgentResponseIngestionResult.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
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

    def test_tampered_response_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        data = result.to_dict()
        data["response"]["summary"] = "Tampered"

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            AgentResponseIngestionResult.from_dict(data)

    def test_tampered_updated_plan_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        data = result.to_dict()
        data["updated_plan"]["objective"] = "Tampered"

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            AgentResponseIngestionResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )
        data = result.to_dict()
        del data["result_hash"]

        with self.assertRaises(
            AgentResponseIngestionIntegrityError
        ):
            AgentResponseIngestionResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        with self.assertRaises(FrozenInstanceError):
            result.status = (  # type: ignore[misc]
                AgentResponseIngestionStatus.ALREADY_INGESTED
            )

    def test_to_journal_returns_independent_journal(self) -> None:
        dispatch = make_dispatch_result()
        response = make_response(dispatch)
        result = AgentResponseIngestion(
            make_request(dispatch, response),
            dispatch,
            response,
        ).ingest(
            dispatch.updated_plan,
            dispatch.to_journal(),
        )

        first = result.to_journal()
        second = result.to_journal()

        self.assertIsNot(first, second)
        self.assertEqual(first.to_jsonl(), second.to_jsonl())


if __name__ == "__main__":
    unittest.main()
