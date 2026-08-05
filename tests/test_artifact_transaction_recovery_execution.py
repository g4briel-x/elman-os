from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elman_os.agent_contracts import canonical_json
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
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
    ArtifactTransactionResult,
)
from elman_os.artifact_transaction_reconciliation import (
    ArtifactTransactionReconciliation,
    ArtifactTransactionReconciliationPolicy,
    ArtifactTransactionReconciliationRequest,
    ArtifactTransactionReconciliationStatus,
    ArtifactTransactionRecoveryStrategy,
)
from elman_os.artifact_transaction_recovery_execution import (
    ArtifactTransactionRecoveryActionKind,
    ArtifactTransactionRecoveryActionResult,
    ArtifactTransactionRecoveryActionStatus,
    ArtifactTransactionRecoveryError,
    ArtifactTransactionRecoveryExecution,
    ArtifactTransactionRecoveryIntegrityError,
    ArtifactTransactionRecoveryLockError,
    ArtifactTransactionRecoveryPolicy,
    ArtifactTransactionRecoveryRequest,
    ArtifactTransactionRecoveryResult,
    ArtifactTransactionRecoveryStatus,
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
RECONCILED = "2026-08-05T01:50:00Z"
RECOVERED = "2026-08-05T02:00:00Z"


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
) -> None:
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
):
    specs = specifications or default_specs()
    prepare_workspace(
        root,
        specs,
        existing_content=existing_content,
    )
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
    return {
        "root": root,
        "specs": specs,
        "plan": plan,
        "verification": verification,
        "preflight": preflight,
        "transaction_policy": transaction_policy,
        "transaction_request": transaction_request,
        "application": application,
    }


def reconcile(context, *, policy=None):
    reconciliation_policy = (
        policy
        or ArtifactTransactionReconciliationPolicy(
            policy_id="policy:transaction-reconciliation-001",
        )
    )
    request = ArtifactTransactionReconciliationRequest.from_sources(
        context["transaction_request"],
        context["plan"],
        context["verification"],
        context["preflight"],
        context["transaction_policy"],
        reconciliation_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=RECONCILED,
    )
    return ArtifactTransactionReconciliation(
        request,
        context["transaction_request"],
        context["plan"],
        context["verification"],
        context["preflight"],
        context["transaction_policy"],
        reconciliation_policy,
    ).reconcile()


def make_execution(
    context,
    reconciliation_result,
    *,
    policy=None,
    requested_at=RECOVERED,
):
    recovery_policy = (
        policy
        or ArtifactTransactionRecoveryPolicy(
            policy_id="policy:transaction-recovery-001",
        )
    )
    request = ArtifactTransactionRecoveryRequest.from_sources(
        reconciliation_result,
        context["transaction_request"],
        context["transaction_policy"],
        context["plan"],
        context["verification"],
        context["preflight"],
        recovery_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=requested_at,
    )
    return ArtifactTransactionRecoveryExecution(
        request,
        reconciliation_result,
        context["transaction_request"],
        context["transaction_policy"],
        context["plan"],
        context["verification"],
        context["preflight"],
        recovery_policy,
    )


def destination(context, index=0) -> Path:
    return (
        context["root"]
        / context["plan"].operations[index].destination_path
    )


def backup(context, index=0) -> Path:
    relative = context["plan"].operations[index].backup_path
    if relative is None:
        raise AssertionError("operation has no backup")
    return context["root"] / relative


def transaction_receipt(context) -> Path:
    return (
        context["root"]
        / context["transaction_policy"].receipt_relative_path(
            context["transaction_request"].transaction_id
        )
    )


def write_payload(context, index=0) -> None:
    payload = context["verification"].payloads[index]
    path = destination(context, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.content)


def write_backup(
    context,
    index=0,
    content: bytes = b"old content\n",
) -> None:
    path = backup(context, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_matching_transaction_lock(context) -> Path:
    path = (
        context["root"]
        / context["transaction_policy"].lock_relative_path
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        canonical_json(
            {
                "transaction_id": (
                    context["transaction_request"].transaction_id
                ),
                "request_hash": (
                    context["transaction_request"].request_hash
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def recovery_receipt(execution) -> Path:
    return (
        Path(execution.request.workspace_root)
        / execution.policy.recovery_receipt_relative_path(
            execution.request.recovery_id
        )
    )


class RecoveryPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        first = ArtifactTransactionRecoveryPolicy(
            policy_id="policy:transaction-recovery-001",
        )
        second = ArtifactTransactionRecoveryPolicy(
            policy_id="policy:transaction-recovery-001",
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self) -> None:
        original = ArtifactTransactionRecoveryPolicy(
            policy_id="policy:transaction-recovery-001",
            max_actions=8,
        )
        restored = ArtifactTransactionRecoveryPolicy.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)

    def test_policy_rejects_zero_actions(self) -> None:
        with self.assertRaises(ArtifactTransactionRecoveryError):
            ArtifactTransactionRecoveryPolicy(
                policy_id="policy:transaction-recovery-001",
                max_actions=0,
            )

    def test_policy_rejects_nested_lock_name(self) -> None:
        with self.assertRaises(ArtifactTransactionRecoveryError):
            ArtifactTransactionRecoveryPolicy(
                policy_id="policy:transaction-recovery-001",
                recovery_lock_name="locks/recovery.lock",
            )

    def test_policy_rejects_non_boolean(self) -> None:
        with self.assertRaises(ArtifactTransactionRecoveryError):
            ArtifactTransactionRecoveryPolicy(
                policy_id="policy:transaction-recovery-001",
                fsync_files="yes",
            )

    def test_policy_receipt_path_is_portable(self) -> None:
        policy = ArtifactTransactionRecoveryPolicy(
            policy_id="policy:transaction-recovery-001",
        )
        path = policy.recovery_receipt_relative_path(
            "transaction-recovery:" + "a" * 64
        )
        self.assertTrue(path.startswith(".elman-os/recoveries/"))
        self.assertTrue(path.endswith(".json"))


class RecoveryRequestTests(unittest.TestCase):
    def test_request_captures_all_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            reconciliation_result = reconcile(context)
            request = make_execution(
                context,
                reconciliation_result,
            ).request
            self.assertEqual(
                request.reconciliation_result_hash,
                reconciliation_result.result_hash,
            )
            self.assertEqual(
                request.transaction_request_hash,
                context["transaction_request"].request_hash,
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            result = reconcile(context)
            first = make_execution(context, result).request
            second = make_execution(context, result).request
            self.assertEqual(first.recovery_id, second.recovery_id)

    def test_request_identifier_is_time_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            result = reconcile(context)
            first = make_execution(context, result).request
            second = make_execution(
                context,
                result,
                requested_at="2026-08-05T03:00:00Z",
            ).request
            self.assertEqual(first.recovery_id, second.recovery_id)
            self.assertNotEqual(first.request_hash, second.request_hash)

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            original = make_execution(
                context,
                reconcile(context),
            ).request
            restored = ArtifactTransactionRecoveryRequest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            request = make_execution(
                context,
                reconcile(context),
            ).request
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionRecoveryIntegrityError
            ):
                ArtifactTransactionRecoveryRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            result = reconcile(context)
            with self.assertRaises(ArtifactTransactionRecoveryError):
                make_execution(
                    context,
                    result,
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            result = reconcile(context)
            request = make_execution(
                context,
                result,
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            ).request
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            request = make_execution(
                context,
                reconcile(context),
            ).request
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class RecoveryConstructionTests(unittest.TestCase):
    def test_clean_reconciliation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            result = reconcile(context)
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CLEAN,
            )
            with self.assertRaises(ArtifactTransactionRecoveryError):
                make_execution(context, result)

    def test_committed_reconciliation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            result = reconcile(context)
            with self.assertRaises(ArtifactTransactionRecoveryError):
                make_execution(context, result)

    def test_matching_recoverable_sources_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            self.assertEqual(
                execution.request.transaction_id,
                context["transaction_request"].transaction_id,
            )

    def test_execution_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            with self.assertRaises(FrozenInstanceError):
                execution.policy = None  # type: ignore[misc]


class CleanupRecoveryTests(unittest.TestCase):
    def test_residual_lock_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            result = execution.execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.COMPLETED,
            )
            self.assertFalse(lock.exists())

    def test_residual_temporary_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            temporary = context["root"] / "src/.elman-write-stale.tmp"
            temporary.write_bytes(b"stale")
            execution = make_execution(context, reconcile(context))
            result = execution.execute()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY,
            )
            self.assertFalse(temporary.exists())

    def test_valid_residual_backup_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(operation=ArtifactOperation.UPDATE),
            )
            write_backup(context)
            backup_path = backup(context)
            execution = make_execution(context, reconcile(context))
            execution.execute()
            self.assertFalse(backup_path.exists())

    def test_cleanup_writes_recovery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            result = execution.execute()
            receipt = recovery_receipt(execution)
            self.assertTrue(receipt.is_file())
            restored = ArtifactTransactionRecoveryResult.from_json(
                receipt.read_text(encoding="utf-8").strip()
            )
            self.assertEqual(restored, result)

    def test_cleanup_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            first = execution.execute()
            second = execution.execute()
            self.assertEqual(first, second)

    def test_cleanup_policy_can_preserve_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = write_matching_transaction_lock(context)
            reconciliation_result = reconcile(context)
            policy = ArtifactTransactionRecoveryPolicy(
                policy_id="policy:transaction-recovery-001",
                remove_reconciliation_controls=False,
            )
            result = make_execution(
                context,
                reconciliation_result,
                policy=policy,
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.NOOP,
            )
            self.assertTrue(lock.exists())


class FinalizeRecoveryTests(unittest.TestCase):
    def test_create_without_receipt_is_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            execution = make_execution(context, reconcile(context))
            result = execution.execute()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT,
            )
            self.assertTrue(transaction_receipt(context).is_file())

    def test_finalized_transaction_receipt_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            execution = make_execution(context, reconcile(context))
            execution.execute()
            receipt = ArtifactTransactionResult.from_json(
                transaction_receipt(context)
                .read_text(encoding="utf-8")
                .strip()
            )
            receipt.verify_hash()
            self.assertEqual(
                receipt.transaction_id,
                context["transaction_request"].transaction_id,
            )

    def test_update_without_receipt_is_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(operation=ArtifactOperation.UPDATE),
            )
            write_payload(context)
            result = make_execution(
                context,
                reconcile(context),
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.COMPLETED,
            )
            self.assertEqual(
                destination(context).read_bytes(),
                b"new payload\n",
            )

    def test_finalize_does_not_rewrite_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            path = destination(context)
            before = path.stat().st_mtime_ns
            make_execution(context, reconcile(context)).execute()
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_finalize_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            execution = make_execution(context, reconcile(context))
            first = execution.execute()
            second = execution.execute()
            self.assertEqual(first, second)

    def test_finalize_fails_if_transaction_receipt_appears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            reconciliation_result = reconcile(context)
            receipt = transaction_receipt(context)
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}", encoding="utf-8")
            result = make_execution(
                context,
                reconciliation_result,
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.FAILED,
            )


class RollbackRecoveryTests(unittest.TestCase):
    def test_partial_create_is_deleted(self) -> None:
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
            result = make_execution(
                context,
                reconcile(context),
            ).execute()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.ROLLBACK,
            )
            self.assertFalse(destination(context, 0).exists())
            self.assertFalse(destination(context, 1).exists())

    def test_partial_update_is_restored(self) -> None:
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
            write_backup(context, 0)
            write_payload(context, 0)
            make_execution(context, reconcile(context)).execute()
            self.assertEqual(
                destination(context, 0).read_bytes(),
                b"old content\n",
            )

    def test_rollback_removes_valid_backup(self) -> None:
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
            write_backup(context, 0)
            write_payload(context, 0)
            backup_path = backup(context, 0)
            make_execution(context, reconcile(context)).execute()
            self.assertFalse(backup_path.exists())

    def test_rollback_replay_is_idempotent(self) -> None:
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
            execution = make_execution(context, reconcile(context))
            first = execution.execute()
            second = execution.execute()
            self.assertEqual(first, second)

    def test_failure_after_create_delete_restores_interrupted_state(
        self,
    ) -> None:
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
            execution = make_execution(context, reconcile(context))
            with patch.object(
                ArtifactTransactionRecoveryExecution,
                "_write_recovery_receipt",
                side_effect=ArtifactTransactionRecoveryError(
                    "injected receipt failure"
                ),
            ):
                result = execution.execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.ROLLED_BACK,
            )
            self.assertEqual(destination(context, 0).read_bytes(), b"a")

    def test_failure_after_update_restore_reinstates_payload(self) -> None:
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
            write_backup(context, 0)
            write_payload(context, 0)
            execution = make_execution(context, reconcile(context))
            with patch.object(
                ArtifactTransactionRecoveryExecution,
                "_write_recovery_receipt",
                side_effect=ArtifactTransactionRecoveryError(
                    "injected receipt failure"
                ),
            ):
                result = execution.execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.ROLLED_BACK,
            )
            self.assertEqual(
                destination(context, 0).read_bytes(),
                b"new-a",
            )


class RecoverySecurityTests(unittest.TestCase):
    def test_existing_recovery_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            lock = (
                context["root"]
                / execution.policy.recovery_lock_relative_path
            )
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("held", encoding="utf-8")
            with self.assertRaises(ArtifactTransactionRecoveryLockError):
                execution.execute()

    def test_changed_destination_after_reconciliation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            reconciliation_result = reconcile(context)
            destination(context).write_bytes(b"changed")
            result = make_execution(
                context,
                reconciliation_result,
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.FAILED,
            )

    def test_changed_control_after_reconciliation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = write_matching_transaction_lock(context)
            reconciliation_result = reconcile(context)
            lock.write_text("changed", encoding="utf-8")
            result = make_execution(
                context,
                reconciliation_result,
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.FAILED,
            )

    def test_changed_backup_after_reconciliation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(operation=ArtifactOperation.UPDATE),
            )
            write_backup(context)
            reconciliation_result = reconcile(context)
            backup(context).write_bytes(b"changed")
            result = make_execution(
                context,
                reconciliation_result,
            ).execute()
            self.assertIs(
                result.status,
                ArtifactTransactionRecoveryStatus.FAILED,
            )

    def test_corrupted_recovery_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            receipt = recovery_receipt(execution)
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}", encoding="utf-8")
            with self.assertRaises(
                ArtifactTransactionRecoveryIntegrityError
            ):
                execution.execute()

    def test_recovery_receipt_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            execution = make_execution(context, reconcile(context))
            receipt = recovery_receipt(execution)
            receipt.parent.mkdir(parents=True)
            target = context["root"] / "target.json"
            target.write_text("{}", encoding="utf-8")
            try:
                receipt.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(
                ArtifactTransactionRecoveryIntegrityError
            ):
                execution.execute()

    def test_result_contains_no_content_execution_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_transaction_lock(context)
            result = make_execution(
                context,
                reconcile(context),
            ).execute()
            payload = result.to_json()
            self.assertNotIn("subprocess", payload)
            self.assertNotIn("execute_artifact", payload)


class RecoveryResultTests(unittest.TestCase):
    def make_result(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = make_context(Path(directory.name))
        write_matching_transaction_lock(context)
        return make_execution(context, reconcile(context)).execute()

    def test_result_json_round_trip(self) -> None:
        original = self.make_result()
        restored = ArtifactTransactionRecoveryResult.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        result = self.make_result()
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

    def test_action_hash_verifies(self) -> None:
        action = self.make_result().actions[0]
        action.verify_hash()

    def test_tampered_action_is_rejected(self) -> None:
        result = self.make_result()
        data = result.to_dict()
        data["actions"][0]["bytes_changed"] = 999
        with self.assertRaises(
            ArtifactTransactionRecoveryIntegrityError
        ):
            ArtifactTransactionRecoveryResult.from_dict(data)

    def test_tampered_count_is_rejected(self) -> None:
        result = self.make_result()
        data = result.to_dict()
        data["applied_count"] = 0
        with self.assertRaises(
            ArtifactTransactionRecoveryIntegrityError
        ):
            ArtifactTransactionRecoveryResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self) -> None:
        result = self.make_result()
        data = result.to_dict()
        data["transaction_id"] = "artifact-transaction:other"
        with self.assertRaises(
            ArtifactTransactionRecoveryIntegrityError
        ):
            ArtifactTransactionRecoveryResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        result = self.make_result()
        data = result.to_dict()
        del data["result_hash"]
        with self.assertRaises(
            ArtifactTransactionRecoveryIntegrityError
        ):
            ArtifactTransactionRecoveryResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        result = self.make_result()
        with self.assertRaises(FrozenInstanceError):
            result.status = (  # type: ignore[misc]
                ArtifactTransactionRecoveryStatus.FAILED
            )

    def test_action_result_is_frozen(self) -> None:
        action = ArtifactTransactionRecoveryActionResult(
            index=0,
            kind=ArtifactTransactionRecoveryActionKind.REMOVE_TEMPORARY,
            target_path="src/file.tmp",
            status=ArtifactTransactionRecoveryActionStatus.SKIPPED,
            before_sha256=None,
            after_sha256=None,
            bytes_changed=0,
            reason="SKIPPED: test",
        )
        with self.assertRaises(FrozenInstanceError):
            action.reason = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
