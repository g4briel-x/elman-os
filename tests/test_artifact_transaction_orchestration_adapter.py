from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from elman_os.agent_contracts import canonical_json
from elman_os.artifact_transaction_lifecycle import (
    ArtifactTransactionLifecyclePhase,
    ArtifactTransactionLifecycleRecord,
    ArtifactTransactionLifecycleRecordStatus,
    ArtifactTransactionLifecycleRequest,
    ArtifactTransactionLifecycleResult,
    ArtifactTransactionLifecycleRoute,
    ArtifactTransactionLifecycleState,
)
from elman_os.artifact_transaction_orchestration_adapter import (
    ArtifactTransactionOrchestrationAdapter,
    ArtifactTransactionOrchestrationDecision,
    ArtifactTransactionOrchestrationError,
    ArtifactTransactionOrchestrationIntegrityError,
    ArtifactTransactionOrchestrationPolicy,
    ArtifactTransactionOrchestrationRequest,
    ArtifactTransactionOrchestrationResult,
    ArtifactTransactionOrchestrationStatus,
)
from elman_os.execution_checkpoint import (
    ExecutionCheckpoint,
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


PLAN_CREATED = "2026-08-05T01:00:00Z"
PLAN_APPROVED = "2026-08-05T01:01:00Z"
PLAN_STARTED = "2026-08-05T01:02:00Z"
STEP_ASSIGNED = "2026-08-05T01:03:00Z"
STEP_STARTED = "2026-08-05T01:04:00Z"
CHECKPOINTED = "2026-08-05T01:05:00Z"
LIFECYCLE_AT = "2026-08-05T01:06:00Z"
INTEGRATED_AT = "2026-08-05T01:07:00Z"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_lifecycle_request(
    root: Path,
    *,
    plan_id: str = "plan:001",
    step_id: str = "step.one",
    agent_id: str = "ELMAN_CORE",
) -> ArtifactTransactionLifecycleRequest:
    return ArtifactTransactionLifecycleRequest(
        lifecycle_id="transaction-lifecycle:" + "a" * 64,
        policy_id="policy:lifecycle-001",
        policy_hash=sha("lifecycle-policy"),
        transaction_id="artifact-transaction:" + "b" * 64,
        transaction_request_hash=sha("transaction-request"),
        transaction_policy_id="policy:transaction-001",
        transaction_policy_hash=sha("transaction-policy"),
        reconciliation_policy_id="policy:reconciliation-001",
        reconciliation_policy_hash=sha("reconciliation-policy"),
        recovery_policy_id="policy:recovery-001",
        recovery_policy_hash=sha("recovery-policy"),
        application_id="artifact-application:001",
        application_plan_hash=sha("application-plan"),
        verification_id="payload-verification:001",
        verification_result_hash=sha("verification-result"),
        preflight_id="workspace-preflight:001",
        preflight_result_hash=sha("preflight-result"),
        snapshot_hash=sha("snapshot"),
        plan_id=plan_id,
        step_id=step_id,
        agent_id=agent_id,
        workspace_root=root.as_posix(),
        requested_by="ELMAN_NEXUS",
        requested_at=LIFECYCLE_AT,
    )


def make_lifecycle_result(
    request: ArtifactTransactionLifecycleRequest,
    state: ArtifactTransactionLifecycleState,
    *,
    route: ArtifactTransactionLifecycleRoute | None = None,
) -> ArtifactTransactionLifecycleResult:
    effective_route = route
    if effective_route is None:
        effective_route = {
            ArtifactTransactionLifecycleState.COMMITTED: (
                ArtifactTransactionLifecycleRoute.APPLY
            ),
            ArtifactTransactionLifecycleState.RECOVERED: (
                ArtifactTransactionLifecycleRoute.RECOVER
            ),
            ArtifactTransactionLifecycleState.CONFLICTED: (
                ArtifactTransactionLifecycleRoute.REFUSE
            ),
            ArtifactTransactionLifecycleState.FAILED: (
                ArtifactTransactionLifecycleRoute.APPLY
            ),
            ArtifactTransactionLifecycleState.APPLY_REQUIRED: (
                ArtifactTransactionLifecycleRoute.INSPECT_ONLY
            ),
            ArtifactTransactionLifecycleState.RECOVERY_REQUIRED: (
                ArtifactTransactionLifecycleRoute.INSPECT_ONLY
            ),
            ArtifactTransactionLifecycleState.CLEAN: (
                ArtifactTransactionLifecycleRoute.INSPECT_ONLY
            ),
        }[state]
    record_status = {
        ArtifactTransactionLifecycleState.CONFLICTED: (
            ArtifactTransactionLifecycleRecordStatus.REFUSED
        ),
        ArtifactTransactionLifecycleState.FAILED: (
            ArtifactTransactionLifecycleRecordStatus.FAILED
        ),
        ArtifactTransactionLifecycleState.APPLY_REQUIRED: (
            ArtifactTransactionLifecycleRecordStatus.DEFERRED
        ),
        ArtifactTransactionLifecycleState.RECOVERY_REQUIRED: (
            ArtifactTransactionLifecycleRecordStatus.DEFERRED
        ),
    }.get(
        state,
        ArtifactTransactionLifecycleRecordStatus.COMPLETED,
    )
    record = ArtifactTransactionLifecycleRecord(
        index=0,
        phase=ArtifactTransactionLifecyclePhase.RECONCILE,
        status=record_status,
        state_before=ArtifactTransactionLifecycleState.CLEAN,
        state_after=state,
        component_id="transaction-reconciliation:001",
        component_result_hash=sha("reconciliation-result"),
        reason=f"{state.value.upper()}: lifecycle test state",
    )
    return ArtifactTransactionLifecycleResult(
        lifecycle_id=request.lifecycle_id,
        final_state=state,
        route=effective_route,
        request_hash=request.request_hash,
        policy_id=request.policy_id,
        policy_hash=request.policy_hash,
        transaction_id=request.transaction_id,
        transaction_request_hash=request.transaction_request_hash,
        application_plan_hash=request.application_plan_hash,
        verification_result_hash=request.verification_result_hash,
        preflight_result_hash=request.preflight_result_hash,
        workspace_root=request.workspace_root,
        records=(record,),
        initial_reconciliation_result_hash=sha(
            "initial-reconciliation"
        ),
        final_reconciliation_result_hash=(
            sha("final-reconciliation")
            if state
            in {
                ArtifactTransactionLifecycleState.RECOVERED,
                ArtifactTransactionLifecycleState.COMMITTED,
            }
            else None
        ),
        transaction_result_hash=(
            sha("transaction-result")
            if state
            is ArtifactTransactionLifecycleState.COMMITTED
            else None
        ),
        recovery_result_hash=(
            sha("recovery-result")
            if state
            is ArtifactTransactionLifecycleState.RECOVERED
            else None
        ),
        transition_count=1,
        completed_at=LIFECYCLE_AT,
        reason=f"{state.value.upper()}: lifecycle result for test",
    )


def make_execution_state(
    *,
    two_steps: bool = False,
    target_status: StepStatus = StepStatus.RUNNING,
    plan_status: PlanStatus = PlanStatus.RUNNING,
    target_agent: str = "ELMAN_CORE",
) -> tuple[ExecutionPlan, ExecutionJournal, ExecutionCheckpoint]:
    steps = []
    if two_steps:
        steps.append(
            ExecutionStep(
                step_id="step.zero",
                title="Prerequisite",
                capability_id="artifact.prepare",
                objective="Prepare source state",
                assigned_agent_id="ELMAN_PREP",
                status=StepStatus.COMPLETED,
            )
        )
    steps.append(
        ExecutionStep(
            step_id="step.one",
            title="Apply artifacts",
            capability_id="artifact.apply",
            objective="Apply verified artifacts",
            dependencies=("step.zero",) if two_steps else (),
            assigned_agent_id=target_agent,
            status=target_status,
        )
    )
    plan = ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Build project artifacts",
        created_by="ELMAN_NEXUS",
        steps=tuple(steps),
        status=plan_status,
        requires_human_approval=True,
        approval_reference="approval:001",
    )
    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        PLAN_CREATED,
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        PLAN_APPROVED,
        payload={"approval_reference": "approval:001"},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        PLAN_STARTED,
        payload={"project_id": plan.project_id},
    )
    if two_steps:
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            STEP_ASSIGNED,
            step_id="step.zero",
            agent_id="ELMAN_PREP",
        )
        journal.append(
            ExecutionEventType.STEP_STARTED,
            STEP_ASSIGNED,
            step_id="step.zero",
            agent_id="ELMAN_PREP",
        )
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            STEP_ASSIGNED,
            step_id="step.zero",
            agent_id="ELMAN_PREP",
        )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        STEP_ASSIGNED,
        step_id="step.one",
        agent_id=target_agent,
    )
    target_event = {
        StepStatus.RUNNING: ExecutionEventType.STEP_STARTED,
        StepStatus.BLOCKED: ExecutionEventType.STEP_BLOCKED,
        StepStatus.FAILED: ExecutionEventType.STEP_FAILED,
        StepStatus.COMPLETED: ExecutionEventType.STEP_COMPLETED,
        StepStatus.APPROVED: ExecutionEventType.STEP_APPROVED,
        StepStatus.PENDING: ExecutionEventType.STEP_READY,
    }[target_status]
    journal.append(
        target_event,
        STEP_STARTED,
        step_id="step.one",
        agent_id=(
            target_agent
            if target_event
            in {
                ExecutionEventType.STEP_STARTED,
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.STEP_FAILED,
                ExecutionEventType.STEP_COMPLETED,
            }
            else None
        ),
    )
    if plan_status is PlanStatus.BLOCKED:
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            STEP_STARTED,
            payload={"affected_step_id": "step.one"},
        )
    elif plan_status is PlanStatus.FAILED:
        journal.append(
            ExecutionEventType.PLAN_FAILED,
            STEP_STARTED,
            payload={"affected_step_id": "step.one"},
        )
    elif plan_status is PlanStatus.COMPLETED:
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            STEP_STARTED,
            payload={"affected_step_id": "step.one"},
        )
    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id="checkpoint:source-001",
        created_at=CHECKPOINTED,
    )
    return plan, journal, checkpoint


def make_adapter(
    root: Path,
    state: ArtifactTransactionLifecycleState,
    *,
    policy: ArtifactTransactionOrchestrationPolicy | None = None,
    two_steps: bool = False,
    execution_state=None,
):
    plan, journal, checkpoint = (
        execution_state
        if execution_state is not None
        else make_execution_state(two_steps=two_steps)
    )
    lifecycle_request = make_lifecycle_request(root)
    lifecycle_result = make_lifecycle_result(
        lifecycle_request,
        state,
    )
    effective_policy = (
        policy
        or ArtifactTransactionOrchestrationPolicy(
            policy_id="policy:orchestration-001",
        )
    )
    request = ArtifactTransactionOrchestrationRequest.from_sources(
        lifecycle_request,
        lifecycle_result,
        plan,
        journal,
        checkpoint,
        effective_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=INTEGRATED_AT,
    )
    adapter = ArtifactTransactionOrchestrationAdapter(
        request,
        lifecycle_request,
        lifecycle_result,
        plan,
        journal,
        checkpoint,
        effective_policy,
    )
    return {
        "adapter": adapter,
        "request": request,
        "policy": effective_policy,
        "lifecycle_request": lifecycle_request,
        "lifecycle_result": lifecycle_result,
        "plan": plan,
        "journal": journal,
        "checkpoint": checkpoint,
    }


class OrchestrationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        first = ArtifactTransactionOrchestrationPolicy(
            policy_id="policy:orchestration-001",
        )
        second = ArtifactTransactionOrchestrationPolicy(
            policy_id="policy:orchestration-001",
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self) -> None:
        original = ArtifactTransactionOrchestrationPolicy(
            policy_id="policy:orchestration-001",
            max_journal_reason_chars=128,
        )
        restored = ArtifactTransactionOrchestrationPolicy.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)

    def test_policy_rejects_zero_reason_limit(self) -> None:
        with self.assertRaises(
            ArtifactTransactionOrchestrationError
        ):
            ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                max_journal_reason_chars=0,
            )

    def test_policy_rejects_non_boolean(self) -> None:
        with self.assertRaises(
            ArtifactTransactionOrchestrationError
        ):
            ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                require_running_step="yes",
            )

    def test_policy_rejects_invalid_identifier(self) -> None:
        with self.assertRaises(
            ArtifactTransactionOrchestrationError
        ):
            ArtifactTransactionOrchestrationPolicy(
                policy_id="bad policy",
            )

    def test_policy_is_frozen(self) -> None:
        policy = ArtifactTransactionOrchestrationPolicy(
            policy_id="policy:orchestration-001",
        )
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class OrchestrationRequestTests(unittest.TestCase):
    def test_request_captures_execution_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            request = context["request"]
            seal = context["journal"].seal()
            self.assertEqual(
                request.source_journal_hash,
                seal.journal_hash,
            )
            self.assertEqual(
                request.source_checkpoint_hash,
                context["checkpoint"].checkpoint_hash,
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            second = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            self.assertEqual(
                first.orchestration_id,
                second.orchestration_id,
            )

    def test_request_identifier_changes_with_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            other_policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-002",
            )
            second = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
                policy=other_policy,
            )["request"]
            self.assertNotEqual(
                first.orchestration_id,
                second.orchestration_id,
            )

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            restored = ArtifactTransactionOrchestrationRequest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationRequest.from_dict(data)

    def test_request_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            with self.assertRaises(
                ArtifactTransactionOrchestrationError
            ):
                ArtifactTransactionOrchestrationRequest.from_sources(
                    context["lifecycle_request"],
                    context["lifecycle_result"],
                    context["plan"],
                    context["journal"],
                    context["checkpoint"],
                    context["policy"],
                    requested_by="ELMAN_NEXUS",
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            request = ArtifactTransactionOrchestrationRequest.from_sources(
                context["lifecycle_request"],
                context["lifecycle_result"],
                context["plan"],
                context["journal"],
                context["checkpoint"],
                context["policy"],
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["request"]
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class OrchestrationConstructionTests(unittest.TestCase):
    def test_adapter_accepts_matching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertEqual(
                context["adapter"].request.step_id,
                "step.one",
            )

    def test_request_rejects_agent_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, journal, checkpoint = make_execution_state(
                target_agent="ELMAN_OTHER"
            )
            lifecycle_request = make_lifecycle_request(root)
            lifecycle_result = make_lifecycle_result(
                lifecycle_request,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
            )
            with self.assertRaises(
                ArtifactTransactionOrchestrationError
            ):
                ArtifactTransactionOrchestrationRequest.from_sources(
                    lifecycle_request,
                    lifecycle_result,
                    plan,
                    journal,
                    checkpoint,
                    policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INTEGRATED_AT,
                )

    def test_request_rejects_result_from_other_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, journal, checkpoint = make_execution_state()
            lifecycle_request = make_lifecycle_request(root)
            other_request = replace(
                lifecycle_request,
                requested_at="2026-08-05T02:00:00Z",
                request_hash=None,
            )
            lifecycle_result = make_lifecycle_result(
                other_request,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
            )
            with self.assertRaises(
                ArtifactTransactionOrchestrationError
            ):
                ArtifactTransactionOrchestrationRequest.from_sources(
                    lifecycle_request,
                    lifecycle_result,
                    plan,
                    journal,
                    checkpoint,
                    policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INTEGRATED_AT,
                )

    def test_request_rejects_incompatible_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            bad_checkpoint = replace(
                context["checkpoint"],
                journal_head_hash="f" * 64,
                checkpoint_hash=None,
            )
            with self.assertRaises(Exception):
                ArtifactTransactionOrchestrationRequest.from_sources(
                    context["lifecycle_request"],
                    context["lifecycle_result"],
                    context["plan"],
                    context["journal"],
                    bad_checkpoint,
                    context["policy"],
                    requested_by="ELMAN_NEXUS",
                    requested_at=INTEGRATED_AT,
                )

    def test_adapter_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"]
            with self.assertRaises(FrozenInstanceError):
                adapter.policy = None  # type: ignore[misc]


class CommittedIntegrationTests(unittest.TestCase):
    def test_committed_lifecycle_completes_single_step_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.COMPLETED,
            )
            self.assertIs(
                result.execution_plan.status,
                PlanStatus.COMPLETED,
            )
            self.assertIs(
                result.execution_plan.get_step("step.one").status,
                StepStatus.COMPLETED,
            )

    def test_committed_single_step_appends_two_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            self.assertEqual(len(result.records), 2)
            self.assertEqual(
                tuple(item.event_type for item in result.records),
                (
                    ExecutionEventType.STEP_COMPLETED,
                    ExecutionEventType.PLAN_COMPLETED,
                ),
            )

    def test_committed_multi_step_keeps_plan_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
                two_steps=True,
            )["adapter"].integrate()
            self.assertIs(
                result.execution_plan.status,
                PlanStatus.COMPLETED,
            )
            self.assertEqual(len(result.records), 2)

    def test_committed_plan_can_remain_running_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                complete_plan_when_all_steps_complete=False,
            )
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
                policy=policy,
            )["adapter"].integrate()
            self.assertIs(
                result.execution_plan.status,
                PlanStatus.RUNNING,
            )
            self.assertEqual(len(result.records), 1)

    def test_journal_payload_binds_lifecycle_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            result = context["adapter"].integrate()
            event = result.execution_journal.events[
                context["journal"].event_count
            ]
            self.assertEqual(
                event.payload["artifact_lifecycle_result_hash"],
                context["lifecycle_result"].result_hash,
            )

    def test_result_checkpoint_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            assessment = result.execution_checkpoint.assess_resume(
                result.execution_plan,
                result.execution_journal,
            )
            self.assertIs(
                assessment.status,
                ResumeAssessmentStatus.TERMINAL,
            )

    def test_source_objects_are_not_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            plan_json = context["plan"].to_json()
            journal_jsonl = context["journal"].to_jsonl()
            checkpoint_json = context["checkpoint"].to_json()
            context["adapter"].integrate()
            self.assertEqual(context["plan"].to_json(), plan_json)
            self.assertEqual(
                context["journal"].to_jsonl(),
                journal_jsonl,
            )
            self.assertEqual(
                context["checkpoint"].to_json(),
                checkpoint_json,
            )

    def test_integration_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            first = context["adapter"].integrate()
            second = context["adapter"].integrate()
            self.assertEqual(first.to_json(), second.to_json())


class NonCommittedPropagationTests(unittest.TestCase):
    def test_recovered_blocks_step_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.RECOVERED,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.BLOCKED,
            )
            self.assertIs(
                result.execution_plan.status,
                PlanStatus.BLOCKED,
            )

    def test_recovered_can_fail_step_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                block_recovered_state=False,
            )
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.RECOVERED,
                policy=policy,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.FAILED,
            )

    def test_conflicted_blocks_step_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.CONFLICTED,
            )["adapter"].integrate()
            self.assertIs(
                result.decision,
                ArtifactTransactionOrchestrationDecision.BLOCK_STEP,
            )
            self.assertIs(
                result.result_step_status,
                StepStatus.BLOCKED,
            )

    def test_conflicted_can_fail_step_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                block_conflicted_state=False,
            )
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.CONFLICTED,
                policy=policy,
            )["adapter"].integrate()
            self.assertIs(
                result.result_step_status,
                StepStatus.FAILED,
            )

    def test_apply_required_blocks_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.APPLY_REQUIRED,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.BLOCKED,
            )

    def test_recovery_required_blocks_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.BLOCKED,
            )

    def test_clean_state_blocks_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.CLEAN,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.BLOCKED,
            )

    def test_deferred_state_can_fail_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                block_deferred_state=False,
            )
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.APPLY_REQUIRED,
                policy=policy,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.FAILED,
            )

    def test_failed_lifecycle_fails_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.FAILED,
            )["adapter"].integrate()
            self.assertIs(
                result.status,
                ArtifactTransactionOrchestrationStatus.FAILED,
            )
            self.assertIs(
                result.execution_plan.status,
                PlanStatus.FAILED,
            )


class IdempotenceAndSafetyTests(unittest.TestCase):
    def test_reintegrated_lifecycle_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            first = context["adapter"].integrate()
            policy = context["policy"]
            second_request = (
                ArtifactTransactionOrchestrationRequest.from_sources(
                    context["lifecycle_request"],
                    context["lifecycle_result"],
                    first.execution_plan,
                    first.execution_journal,
                    first.execution_checkpoint,
                    policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INTEGRATED_AT,
                )
            )
            second = ArtifactTransactionOrchestrationAdapter(
                second_request,
                context["lifecycle_request"],
                context["lifecycle_result"],
                first.execution_plan,
                first.execution_journal,
                first.execution_checkpoint,
                policy,
            ).integrate()
            self.assertIs(
                second.status,
                ArtifactTransactionOrchestrationStatus.NOOP,
            )
            self.assertEqual(second.records, ())

    def test_noop_preserves_all_state_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            first = context["adapter"].integrate()
            request = ArtifactTransactionOrchestrationRequest.from_sources(
                context["lifecycle_request"],
                context["lifecycle_result"],
                first.execution_plan,
                first.execution_journal,
                first.execution_checkpoint,
                context["policy"],
                requested_by="ELMAN_NEXUS",
                requested_at=INTEGRATED_AT,
            )
            result = ArtifactTransactionOrchestrationAdapter(
                request,
                context["lifecycle_request"],
                context["lifecycle_result"],
                first.execution_plan,
                first.execution_journal,
                first.execution_checkpoint,
                context["policy"],
            ).integrate()
            self.assertEqual(
                result.source_plan_state_hash,
                result.result_plan_state_hash,
            )
            self.assertEqual(
                result.source_journal_hash,
                result.result_journal_hash,
            )
            self.assertEqual(
                result.source_checkpoint_hash,
                result.result_checkpoint_hash,
            )

    def test_non_running_step_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = make_execution_state(
                target_status=StepStatus.BLOCKED,
                plan_status=PlanStatus.BLOCKED,
            )
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
                execution_state=state,
            )
            with self.assertRaises(
                ArtifactTransactionOrchestrationError
            ):
                context["adapter"].integrate()

    def test_workspace_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            make_adapter(
                root,
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_journal_reason_is_truncated_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionOrchestrationPolicy(
                policy_id="policy:orchestration-001",
                max_journal_reason_chars=8,
            )
            context = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
                policy=policy,
            )
            result = context["adapter"].integrate()
            event = result.execution_journal.events[
                context["journal"].event_count
            ]
            self.assertLessEqual(
                len(event.payload["lifecycle_reason"]),
                8,
            )


class OrchestrationResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            restored = ArtifactTransactionOrchestrationResult.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
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

    def test_record_hash_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate().records[0]
            record.verify_hash()

    def test_tampered_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            data["records"][0]["reason"] = "changed"
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_tampered_plan_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            plan_data = json.loads(data["updated_plan_json"])
            plan_data["project_id"] = "project:other"
            data["updated_plan_json"] = canonical_json(plan_data)
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_tampered_journal_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            data["result_journal_hash"] = "f" * 64
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_tampered_checkpoint_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            data["result_checkpoint_hash"] = "f" * 64
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactTransactionOrchestrationIntegrityError
            ):
                ArtifactTransactionOrchestrationResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_adapter(
                Path(directory),
                ArtifactTransactionLifecycleState.COMMITTED,
            )["adapter"].integrate()
            with self.assertRaises(FrozenInstanceError):
                result.reason = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
