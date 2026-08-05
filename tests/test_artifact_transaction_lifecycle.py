from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elman_os.agent_output_validation import (
    AgentOutputValidationResult,
    AgentOutputValidationStatus,
    ArtifactClassification,
    ArtifactOperation,
    ArtifactValidationDecision,
    ArtifactValidationRecord,
)
from elman_os.artifact_application_plan import (
    ArtifactApplicationPolicy,
    ArtifactApplicationRequest,
    build_artifact_application_plan,
)
from elman_os.artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerification,
    ArtifactPayloadVerificationPolicy,
    ArtifactPayloadVerificationRequest,
)
from elman_os.artifact_transaction_application import (
    ArtifactTransactionApplication,
    ArtifactTransactionError,
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
)
from elman_os.artifact_transaction_lifecycle import (
    ArtifactTransactionLifecycleCoordinator,
    ArtifactTransactionLifecycleError,
    ArtifactTransactionLifecycleIntegrityError,
    ArtifactTransactionLifecyclePhase,
    ArtifactTransactionLifecyclePolicy,
    ArtifactTransactionLifecycleRecordStatus,
    ArtifactTransactionLifecycleRequest,
    ArtifactTransactionLifecycleResult,
    ArtifactTransactionLifecycleRoute,
    ArtifactTransactionLifecycleState,
)
from elman_os.artifact_transaction_reconciliation import (
    ArtifactTransactionReconciliationPolicy,
)
from elman_os.artifact_transaction_recovery_execution import (
    ArtifactTransactionRecoveryPolicy,
)
from elman_os.artifact_workspace_preflight import (
    ArtifactWorkspacePreflight,
    ArtifactWorkspacePreflightPolicy,
    ArtifactWorkspacePreflightRequest,
)


VALIDATED = "2026-08-05T01:00:00Z"
PLANNED = "2026-08-05T01:10:00Z"
VERIFIED = "2026-08-05T01:20:00Z"
INSPECTED = "2026-08-05T01:30:00Z"
TRANSACTION_REQUESTED = "2026-08-05T01:40:00Z"
LIFECYCLE_REQUESTED = "2026-08-05T01:50:00Z"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def default_specs(
    *,
    operation: ArtifactOperation = ArtifactOperation.CREATE,
    path: str = "src/generated.txt",
    content: bytes = b"new payload\n",
):
    return (
        (
            path,
            content,
            "text/plain",
            ArtifactClassification.SOURCE,
            operation,
        ),
    )


def make_validation_result(specifications):
    records = tuple(
        ArtifactValidationRecord(
            index=index,
            path=path,
            decision=ArtifactValidationDecision.ACCEPTED,
            classification=classification,
            operation=operation,
            sha256=sha256(content),
            size_bytes=len(content),
            media_type=media_type,
            reasons=(
                "ACCEPTED: artifact declaration satisfies policy",
            ),
        )
        for index, (
            path,
            content,
            media_type,
            classification,
            operation,
        ) in enumerate(specifications)
    )
    return AgentOutputValidationResult(
        validation_id="output-validation:001",
        status=AgentOutputValidationStatus.ACCEPTED,
        request_hash="1" * 64,
        policy_id="policy:output-validation-001",
        policy_hash="2" * 64,
        ingestion_id="ingestion:001",
        ingestion_result_hash="3" * 64,
        plan_id="plan:001",
        step_id="step.one",
        agent_request_id="agent-request:001",
        agent_id="ELMAN_CORE",
        response_hash="4" * 64,
        records=records,
        top_level_reasons=(),
        accepted_count=len(records),
        review_count=0,
        rejected_count=0,
        total_declared_bytes=sum(
            len(content)
            for _, content, _, _, _ in specifications
        ),
        validated_at=VALIDATED,
        plan_state_hash="5" * 64,
        journal_event_count=12,
        journal_head_hash="6" * 64,
        journal_hash="7" * 64,
    )


def prepare_workspace(
    root: Path,
    specifications,
    *,
    existing_content: bytes = b"old content\n",
):
    for path, _, _, _, operation in specifications:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if operation is ArtifactOperation.UPDATE:
            destination.write_bytes(existing_content)


def make_context(
    root: Path,
    specifications=None,
    *,
    existing_content: bytes = b"old content\n",
    lifecycle_policy=None,
):
    specs = specifications or default_specs()
    prepare_workspace(root, specs, existing_content=existing_content)
    validation = make_validation_result(specs)

    application_policy = ArtifactApplicationPolicy(
        policy_id="policy:artifact-application-001",
    )
    has_update = any(
        operation is ArtifactOperation.UPDATE
        for _, _, _, _, operation in specs
    )
    application_request = (
        ArtifactApplicationRequest.from_validation_result(
            validation,
            application_policy,
            requested_by="ELMAN_NEXUS",
            requested_at=PLANNED,
            approval_reference=(
                "approval:update-001" if has_update else None
            ),
        )
    )
    plan = build_artifact_application_plan(
        application_request,
        validation,
        application_policy,
    )

    by_path = {
        path: (content, media_type)
        for path, content, media_type, _, _ in specs
    }
    payloads = tuple(
        ArtifactPayload(
            operation_id=operation.operation_id,
            destination_path=operation.destination_path,
            media_type=operation.media_type,
            content=by_path[operation.destination_path][0],
        )
        for operation in plan.operations
    )
    verification_policy = ArtifactPayloadVerificationPolicy(
        policy_id="policy:payload-verification-001",
        review_media_types=(),
    )
    verification_request = (
        ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            verification_policy,
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
    )
    verification = ArtifactPayloadVerification(
        verification_request,
        plan,
        payloads,
        verification_policy,
    ).verify()

    preflight_policy = ArtifactWorkspacePreflightPolicy(
        policy_id="policy:workspace-preflight-001",
    )
    preflight_request = ArtifactWorkspacePreflightRequest.from_sources(
        plan,
        verification,
        preflight_policy,
        workspace_root=root,
        requested_by="ELMAN_NEXUS",
        requested_at=INSPECTED,
    )
    preflight = ArtifactWorkspacePreflight(
        preflight_request,
        plan,
        verification,
        preflight_policy,
    ).inspect()

    transaction_policy = ArtifactTransactionPolicy(
        policy_id="policy:transaction-001",
    )
    transaction_request = ArtifactTransactionRequest.from_sources(
        plan,
        verification,
        preflight,
        transaction_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=TRANSACTION_REQUESTED,
    )
    application = ArtifactTransactionApplication(
        transaction_request,
        plan,
        verification,
        preflight,
        transaction_policy,
    )

    reconciliation_policy = ArtifactTransactionReconciliationPolicy(
        policy_id="policy:transaction-reconciliation-001",
    )
    recovery_policy = ArtifactTransactionRecoveryPolicy(
        policy_id="policy:transaction-recovery-001",
    )
    policy = lifecycle_policy or ArtifactTransactionLifecyclePolicy(
        policy_id="policy:transaction-lifecycle-001",
    )
    lifecycle_request = ArtifactTransactionLifecycleRequest.from_sources(
        transaction_request,
        transaction_policy,
        plan,
        verification,
        preflight,
        reconciliation_policy,
        recovery_policy,
        policy,
        requested_by="ELMAN_NEXUS",
        requested_at=LIFECYCLE_REQUESTED,
    )
    coordinator = ArtifactTransactionLifecycleCoordinator(
        lifecycle_request,
        transaction_request,
        transaction_policy,
        plan,
        verification,
        preflight,
        reconciliation_policy,
        recovery_policy,
        policy,
    )
    return {
        "root": root,
        "specs": specs,
        "plan": plan,
        "verification": verification,
        "preflight": preflight,
        "transaction_policy": transaction_policy,
        "transaction_request": transaction_request,
        "application": application,
        "reconciliation_policy": reconciliation_policy,
        "recovery_policy": recovery_policy,
        "lifecycle_policy": policy,
        "lifecycle_request": lifecycle_request,
        "coordinator": coordinator,
    }


def destination_for(context, index=0):
    return context["root"] / context["plan"].operations[index].destination_path


def backup_for(context, index=0):
    relative = context["plan"].operations[index].backup_path
    if relative is None:
        raise AssertionError("operation has no backup")
    return context["root"] / relative


def lock_for(context):
    return (
        context["root"]
        / context["transaction_policy"].lock_relative_path
    )


def write_matching_lock(context):
    path = lock_for(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "transaction_id": (
                    context["transaction_request"].transaction_id
                ),
                "request_hash": (
                    context["transaction_request"].request_hash
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def write_payload(context, index=0):
    destination_for(context, index).write_bytes(
        context["verification"].payloads[index].content
    )


def write_valid_backup(context, index=0, content=b"old content\n"):
    path = backup_for(context, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class LifecyclePolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        first = ArtifactTransactionLifecyclePolicy(
            policy_id="policy:transaction-lifecycle-001"
        )
        second = ArtifactTransactionLifecyclePolicy(
            policy_id="policy:transaction-lifecycle-001"
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self):
        original = ArtifactTransactionLifecyclePolicy(
            policy_id="policy:transaction-lifecycle-001",
            apply_after_recovery=True,
        )
        restored = ArtifactTransactionLifecyclePolicy.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)

    def test_policy_rejects_non_boolean(self):
        with self.assertRaises(ArtifactTransactionLifecycleError):
            ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                auto_apply_when_clean="yes",
            )

    def test_policy_rejects_small_transition_limit(self):
        with self.assertRaises(ArtifactTransactionLifecycleError):
            ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                max_transitions=1,
            )

    def test_policy_rejects_invalid_identifier(self):
        with self.assertRaises(ArtifactTransactionLifecycleError):
            ArtifactTransactionLifecyclePolicy(policy_id="bad policy")


class LifecycleRequestTests(unittest.TestCase):
    def test_request_captures_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            request = context["lifecycle_request"]
            self.assertEqual(
                request.transaction_request_hash,
                context["transaction_request"].request_hash,
            )
            self.assertEqual(
                request.application_plan_hash,
                context["plan"].plan_hash,
            )

    def test_request_identifier_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_context(root)["lifecycle_request"]
            second = make_context(root)["lifecycle_request"]
            self.assertEqual(first.lifecycle_id, second.lifecycle_id)

    def test_request_identifier_is_time_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            first = context["lifecycle_request"]
            second = ArtifactTransactionLifecycleRequest.from_sources(
                context["transaction_request"],
                context["transaction_policy"],
                context["plan"],
                context["verification"],
                context["preflight"],
                context["reconciliation_policy"],
                context["recovery_policy"],
                context["lifecycle_policy"],
                requested_by="ELMAN_NEXUS",
                requested_at="2026-08-05T02:00:00Z",
            )
            self.assertEqual(first.lifecycle_id, second.lifecycle_id)
            self.assertNotEqual(first.request_hash, second.request_hash)

    def test_request_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = make_context(Path(directory))["lifecycle_request"]
            restored = ArtifactTransactionLifecycleRequest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["lifecycle_request"]
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleRequest.from_dict(data)

    def test_request_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["lifecycle_request"]
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            with self.assertRaises(ArtifactTransactionLifecycleError):
                ArtifactTransactionLifecycleRequest.from_sources(
                    context["transaction_request"],
                    context["transaction_policy"],
                    context["plan"],
                    context["verification"],
                    context["preflight"],
                    context["reconciliation_policy"],
                    context["recovery_policy"],
                    context["lifecycle_policy"],
                    requested_by="ELMAN_NEXUS",
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            request = ArtifactTransactionLifecycleRequest.from_sources(
                context["transaction_request"],
                context["transaction_policy"],
                context["plan"],
                context["verification"],
                context["preflight"],
                context["reconciliation_policy"],
                context["recovery_policy"],
                context["lifecycle_policy"],
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["lifecycle_request"]
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class LifecycleConstructionTests(unittest.TestCase):
    def test_coordinator_accepts_matching_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = make_context(Path(directory))["coordinator"]
            self.assertEqual(
                coordinator.request.transaction_id,
                coordinator.transaction_request.transaction_id,
            )

    def test_coordinator_rejects_other_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            other = ArtifactTransactionLifecyclePolicy(
                policy_id="policy:other"
            )
            with self.assertRaises(ArtifactTransactionLifecycleError):
                ArtifactTransactionLifecycleCoordinator(
                    context["lifecycle_request"],
                    context["transaction_request"],
                    context["transaction_policy"],
                    context["plan"],
                    context["verification"],
                    context["preflight"],
                    context["reconciliation_policy"],
                    context["recovery_policy"],
                    other,
                )

    def test_coordinator_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = make_context(Path(directory))["coordinator"]
            with self.assertRaises(FrozenInstanceError):
                coordinator.policy = None  # type: ignore[misc]


class CleanLifecycleTests(unittest.TestCase):
    def test_clean_create_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertIs(result.route, ArtifactTransactionLifecycleRoute.APPLY)
            self.assertEqual(
                destination_for(context).read_bytes(),
                b"new payload\n",
            )

    def test_clean_update_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(operation=ArtifactOperation.UPDATE),
            )
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertEqual(
                destination_for(context).read_bytes(),
                b"new payload\n",
            )

    def test_clean_policy_can_defer_application(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                auto_apply_when_clean=False,
            )
            context = make_context(
                Path(directory),
                lifecycle_policy=policy,
            )
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.APPLY_REQUIRED,
            )
            self.assertFalse(destination_for(context).exists())

    def test_clean_route_has_reconcile_then_application(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            self.assertEqual(
                tuple(record.phase for record in result.records),
                (
                    ArtifactTransactionLifecyclePhase.RECONCILE,
                    ArtifactTransactionLifecyclePhase.APPLICATION,
                ),
            )

    def test_committed_replay_result_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["coordinator"].run()
            second = context["coordinator"].run()
            third = context["coordinator"].run()
            self.assertEqual(second.to_json(), third.to_json())


class CommittedLifecycleTests(unittest.TestCase):
    def test_committed_transaction_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertIs(
                result.route,
                ArtifactTransactionLifecycleRoute.VERIFY_COMMITTED,
            )

    def test_committed_replay_does_not_rewrite_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            destination = destination_for(context)
            before = destination.stat().st_mtime_ns
            context["coordinator"].run()
            self.assertEqual(destination.stat().st_mtime_ns, before)

    def test_committed_result_contains_transaction_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            result = context["coordinator"].run()
            self.assertIsNotNone(result.transaction_result_hash)

    def test_committed_tamper_is_refused_by_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            destination_for(context).write_bytes(b"tampered")
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.CONFLICTED,
            )
            self.assertIs(
                result.route,
                ArtifactTransactionLifecycleRoute.REFUSE,
            )


class RecoverableLifecycleTests(unittest.TestCase):
    def test_residual_lock_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_lock(context)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.RECOVERED,
            )
            self.assertFalse(lock_for(context).exists())

    def test_recovery_policy_can_defer(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                auto_recover_when_recoverable=False,
            )
            context = make_context(
                Path(directory),
                lifecycle_policy=policy,
            )
            write_matching_lock(context)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.RECOVERY_REQUIRED,
            )
            self.assertTrue(lock_for(context).exists())

    def test_applied_create_without_receipt_is_finalized(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertIs(result.route, ArtifactTransactionLifecycleRoute.RECOVER)

    def test_partial_create_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = (
                (
                    "src/a.txt",
                    b"a",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
                (
                    "src/b.txt",
                    b"b",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
            )
            context = make_context(Path(directory), specs)
            write_payload(context, 0)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.RECOVERED,
            )
            self.assertFalse(destination_for(context, 0).exists())

    def test_partial_update_with_backup_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = (
                (
                    "src/a.txt",
                    b"new-a",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.UPDATE,
                ),
                (
                    "src/b.txt",
                    b"new-b",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.UPDATE,
                ),
            )
            context = make_context(Path(directory), specs)
            write_valid_backup(context, 0)
            write_payload(context, 0)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.RECOVERED,
            )
            self.assertEqual(
                destination_for(context, 0).read_bytes(),
                b"old content\n",
            )

    def test_apply_after_recovery_recommits(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = (
                (
                    "src/a.txt",
                    b"a",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
                (
                    "src/b.txt",
                    b"b",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
            )
            policy = ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                apply_after_recovery=True,
            )
            context = make_context(
                Path(directory),
                specs,
                lifecycle_policy=policy,
            )
            write_payload(context, 0)
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.COMMITTED,
            )
            self.assertIs(
                result.route,
                ArtifactTransactionLifecycleRoute.RECOVER_THEN_APPLY,
            )
            self.assertEqual(destination_for(context, 0).read_bytes(), b"a")
            self.assertEqual(destination_for(context, 1).read_bytes(), b"b")

    def test_recovery_result_hash_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_lock(context)
            result = context["coordinator"].run()
            self.assertIsNotNone(result.recovery_result_hash)

    def test_recovery_records_are_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_lock(context)
            result = context["coordinator"].run()
            self.assertEqual(
                tuple(record.index for record in result.records),
                tuple(range(len(result.records))),
            )


class ConflictedLifecycleTests(unittest.TestCase):
    def test_unknown_destination_content_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            destination_for(context).write_bytes(b"unknown")
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.CONFLICTED,
            )

    def test_invalid_lock_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = lock_for(context)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("{}", encoding="utf-8")
            result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.CONFLICTED,
            )
            self.assertTrue(lock.exists())

    def test_conflict_has_refused_record(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            destination_for(context).write_bytes(b"unknown")
            result = context["coordinator"].run()
            self.assertIs(
                result.records[-1].state_after,
                ArtifactTransactionLifecycleState.CONFLICTED,
            )


class LifecycleFailureTests(unittest.TestCase):
    def test_application_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            with patch.object(
                ArtifactTransactionApplication,
                "_commit_create",
                side_effect=ArtifactTransactionError("injected failure"),
            ):
                result = context["coordinator"].run()
            self.assertIs(
                result.final_state,
                ArtifactTransactionLifecycleState.FAILED,
            )

    def test_transition_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionLifecyclePolicy(
                policy_id="policy:transaction-lifecycle-001",
                max_transitions=2,
            )
            context = make_context(
                Path(directory),
                lifecycle_policy=policy,
            )
            write_matching_lock(context)
            with self.assertRaises(ArtifactTransactionLifecycleError):
                context["coordinator"].run()


class LifecycleResultTests(unittest.TestCase):
    def test_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = make_context(Path(directory))["coordinator"].run()
            restored = ArtifactTransactionLifecycleResult.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_json_is_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
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

    def test_record_hash_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            record = make_context(Path(directory))[
                "coordinator"
            ].run().records[0]
            record.verify_hash()

    def test_tampered_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            data = result.to_dict()
            data["records"][0]["reason"] = "changed"
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleResult.from_dict(data)

    def test_tampered_transition_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            data = result.to_dict()
            data["transition_count"] = 0
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactTransactionLifecycleIntegrityError
            ):
                ArtifactTransactionLifecycleResult.from_dict(data)

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(Path(directory))["coordinator"].run()
            with self.assertRaises(FrozenInstanceError):
                result.final_state = (  # type: ignore[misc]
                    ArtifactTransactionLifecycleState.FAILED
                )


if __name__ == "__main__":
    unittest.main()
