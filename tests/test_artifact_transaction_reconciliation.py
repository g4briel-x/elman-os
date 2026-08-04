from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

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
)
from elman_os.artifact_transaction_reconciliation import (
    ArtifactTransactionBackupState,
    ArtifactTransactionControlKind,
    ArtifactTransactionControlState,
    ArtifactTransactionDestinationState,
    ArtifactTransactionReconciliation,
    ArtifactTransactionReconciliationError,
    ArtifactTransactionReconciliationIntegrityError,
    ArtifactTransactionReconciliationPolicy,
    ArtifactTransactionReconciliationRequest,
    ArtifactTransactionReconciliationResult,
    ArtifactTransactionReconciliationStatus,
    ArtifactTransactionRecoveryAction,
    ArtifactTransactionRecoveryStrategy,
)
from elman_os.artifact_workspace_preflight import (
    ArtifactWorkspacePreflight,
    ArtifactWorkspacePreflightPolicy,
    ArtifactWorkspacePreflightRequest,
)


VALIDATED = "2026-08-05T00:00:00Z"
PLANNED = "2026-08-05T00:10:00Z"
VERIFIED = "2026-08-05T00:20:00Z"
INSPECTED = "2026-08-05T00:30:00Z"
TRANSACTION_REQUESTED = "2026-08-05T00:40:00Z"
RECONCILED = "2026-08-05T00:50:00Z"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def default_specs(
    *,
    operation: ArtifactOperation = ArtifactOperation.CREATE,
    path: str = "src/generated.txt",
    content: bytes = b"new payload\n",
) -> tuple[
    tuple[
        str,
        bytes,
        str,
        ArtifactClassification,
        ArtifactOperation,
    ],
    ...,
]:
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
    transaction_policy=None,
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
                "approval:update-001"
                if has_update
                else None
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

    tx_policy = transaction_policy or ArtifactTransactionPolicy(
        policy_id="policy:transaction-001",
    )
    tx_request = ArtifactTransactionRequest.from_sources(
        plan,
        verification,
        preflight,
        tx_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=TRANSACTION_REQUESTED,
    )
    application = ArtifactTransactionApplication(
        tx_request,
        plan,
        verification,
        preflight,
        tx_policy,
    )
    return {
        "root": root,
        "specs": specs,
        "plan": plan,
        "verification": verification,
        "preflight": preflight,
        "transaction_policy": tx_policy,
        "transaction_request": tx_request,
        "application": application,
    }


def make_reconciler(
    context,
    *,
    policy=None,
    requested_at=RECONCILED,
):
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
        requested_at=requested_at,
    )
    reconciler = ArtifactTransactionReconciliation(
        request,
        context["transaction_request"],
        context["plan"],
        context["verification"],
        context["preflight"],
        context["transaction_policy"],
        reconciliation_policy,
    )
    return reconciler


def destination_for(context, index=0) -> Path:
    return (
        context["root"]
        / context["plan"].operations[index].destination_path
    )


def backup_for(context, index=0) -> Path:
    relative = context["plan"].operations[index].backup_path
    if relative is None:
        raise AssertionError("operation has no backup")
    return context["root"] / relative


def receipt_for(context) -> Path:
    policy = context["transaction_policy"]
    request = context["transaction_request"]
    return (
        context["root"]
        / policy.receipt_relative_path(request.transaction_id)
    )


def lock_for(context) -> Path:
    return (
        context["root"]
        / context["transaction_policy"].lock_relative_path
    )


def write_matching_lock(context) -> Path:
    path = lock_for(context)
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


def write_payload(context, index=0) -> None:
    operation = context["plan"].operations[index]
    payload = context["verification"].payloads[index]
    destination = context["root"] / operation.destination_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload.content)


def write_valid_backup(
    context,
    index=0,
    *,
    content: bytes = b"old content\n",
) -> None:
    backup = backup_for(context, index)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(content)


def tree_snapshot(root: Path) -> dict[str, tuple[bytes | None, int]]:
    result = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            result[relative] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
        else:
            result[relative] = (None, 0)
    return result


class ReconciliationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        first = ArtifactTransactionReconciliationPolicy(
            policy_id="policy:transaction-reconciliation-001",
        )
        second = ArtifactTransactionReconciliationPolicy(
            policy_id="policy:transaction-reconciliation-001",
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self) -> None:
        original = ArtifactTransactionReconciliationPolicy(
            policy_id="policy:transaction-reconciliation-001",
            max_temporary_entries=8,
        )
        restored = ArtifactTransactionReconciliationPolicy.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)

    def test_policy_rejects_zero_control_limit(self) -> None:
        with self.assertRaises(
            ArtifactTransactionReconciliationError
        ):
            ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                max_control_file_bytes=0,
            )

    def test_policy_rejects_zero_temp_limit(self) -> None:
        with self.assertRaises(
            ArtifactTransactionReconciliationError
        ):
            ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                max_temporary_entries=0,
            )

    def test_policy_rejects_non_boolean_option(self) -> None:
        with self.assertRaises(
            ArtifactTransactionReconciliationError
        ):
            ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                allow_finalize_without_receipt="yes",
            )

    def test_policy_rejects_invalid_identifier(self) -> None:
        with self.assertRaises(
            ArtifactTransactionReconciliationError
        ):
            ArtifactTransactionReconciliationPolicy(
                policy_id="bad policy",
            )


class ReconciliationRequestTests(unittest.TestCase):
    def test_request_captures_all_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            request = make_reconciler(context).request
            self.assertEqual(
                request.transaction_request_hash,
                context["transaction_request"].request_hash,
            )
            self.assertEqual(
                request.application_plan_hash,
                context["plan"].plan_hash,
            )
            self.assertEqual(
                request.preflight_result_hash,
                context["preflight"].result_hash,
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            first = make_reconciler(context).request
            second = make_reconciler(context).request
            self.assertEqual(
                first.reconciliation_id,
                second.reconciliation_id,
            )

    def test_request_identifier_is_time_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            first = make_reconciler(context).request
            second = make_reconciler(
                context,
                requested_at="2026-08-05T01:00:00Z",
            ).request
            self.assertEqual(
                first.reconciliation_id,
                second.reconciliation_id,
            )
            self.assertNotEqual(
                first.request_hash,
                second.request_hash,
            )

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_reconciler(
                make_context(Path(directory))
            ).request
            restored = (
                ArtifactTransactionReconciliationRequest.from_json(
                    original.to_json()
                )
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_reconciler(
                make_context(Path(directory))
            ).request
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationRequest.from_dict(
                    data
                )

    def test_request_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_reconciler(
                make_context(Path(directory))
            ).request
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationRequest.from_dict(
                    data
                )

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            with self.assertRaises(
                ArtifactTransactionReconciliationError
            ):
                make_reconciler(
                    context,
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_reconciler(
                make_context(Path(directory)),
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            ).request
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_reconciler(
                make_context(Path(directory))
            ).request
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class ReconciliationConstructionTests(unittest.TestCase):
    def test_reconciler_accepts_matching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reconciler = make_reconciler(
                make_context(Path(directory))
            )
            self.assertEqual(
                reconciler.request.transaction_id,
                reconciler.transaction_request.transaction_id,
            )

    def test_reconciler_rejects_other_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            reconciler = make_reconciler(context)
            other = ArtifactTransactionReconciliationPolicy(
                policy_id="policy:other",
            )
            with self.assertRaises(
                ArtifactTransactionReconciliationError
            ):
                ArtifactTransactionReconciliation(
                    reconciler.request,
                    context["transaction_request"],
                    context["plan"],
                    context["verification"],
                    context["preflight"],
                    context["transaction_policy"],
                    other,
                )

    def test_reconciler_rejects_other_transaction_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_context(root)
            reconciler = make_reconciler(first)
            second = make_context(
                root,
                default_specs(path="docs/other.txt"),
            )
            with self.assertRaises(
                ArtifactTransactionReconciliationError
            ):
                ArtifactTransactionReconciliation(
                    reconciler.request,
                    second["transaction_request"],
                    first["plan"],
                    first["verification"],
                    first["preflight"],
                    first["transaction_policy"],
                    reconciler.policy,
                )

    def test_reconciler_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reconciler = make_reconciler(
                make_context(Path(directory))
            )
            with self.assertRaises(FrozenInstanceError):
                reconciler.policy = None  # type: ignore[misc]


class CleanReconciliationTests(unittest.TestCase):
    def test_unstarted_create_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CLEAN,
            )
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.NONE,
            )

    def test_unstarted_update_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CLEAN,
            )
            self.assertEqual(result.before_count, 1)

    def test_clean_record_reports_before_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            self.assertIs(
                result.records[0].destination_state,
                ArtifactTransactionDestinationState.BEFORE,
            )
            self.assertIs(
                result.records[0].action,
                ArtifactTransactionRecoveryAction.NONE,
            )

    def test_clean_control_entries_include_absent_lock_and_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            self.assertEqual(
                tuple(
                    (entry.kind, entry.state)
                    for entry in result.control_entries[:2]
                ),
                (
                    (
                        ArtifactTransactionControlKind.LOCK,
                        ArtifactTransactionControlState.ABSENT,
                    ),
                    (
                        ArtifactTransactionControlKind.RECEIPT,
                        ArtifactTransactionControlState.ABSENT,
                    ),
                ),
            )

    def test_reconciliation_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reconciler = make_reconciler(make_context(root))
            before = tree_snapshot(root)
            reconciler.reconcile()
            self.assertEqual(tree_snapshot(root), before)


class CommittedReconciliationTests(unittest.TestCase):
    def test_committed_create_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.COMMITTED,
            )
            self.assertEqual(result.after_count, 1)

    def test_committed_update_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            context["application"].apply()
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.COMMITTED,
            )
            self.assertIs(
                result.records[0].backup_state,
                ArtifactTransactionBackupState.VALID,
            )

    def test_committed_receipt_is_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            result = make_reconciler(context).reconcile()
            receipt = next(
                entry
                for entry in result.control_entries
                if entry.kind
                is ArtifactTransactionControlKind.RECEIPT
            )
            self.assertIs(
                receipt.state,
                ArtifactTransactionControlState.MATCHING,
            )

    def test_committed_with_residual_lock_plans_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            write_matching_lock(context)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.COMMITTED,
            )
            self.assertIn(
                "REMOVE_RESIDUAL_LOCK:"
                + context["transaction_policy"].lock_relative_path,
                result.control_actions,
            )

    def test_committed_with_residual_temp_plans_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            temp = context["root"] / "src/.elman-write-test.tmp"
            temp.write_bytes(b"temporary")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.COMMITTED,
            )
            self.assertTrue(
                any(
                    action.startswith("REMOVE_TEMPORARY:")
                    for action in result.control_actions
                )
            )

    def test_committed_result_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            reconciler = make_reconciler(context)
            first = reconciler.reconcile()
            second = reconciler.reconcile()
            self.assertEqual(first.to_json(), second.to_json())


class RecoverableReconciliationTests(unittest.TestCase):
    def test_matching_residual_lock_is_cleanup_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_lock(context)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.RECOVERABLE,
            )
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY,
            )

    def test_residual_temporary_is_cleanup_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            temp = context["root"] / "src/.elman-write-stale.tmp"
            temp.write_bytes(b"stale")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.RECOVERABLE,
            )
            self.assertTrue(
                any(
                    action.startswith("REMOVE_TEMPORARY:")
                    for action in result.control_actions
                )
            )

    def test_valid_unneeded_backup_is_cleanup_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            write_valid_backup(context)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.CLEANUP_ONLY,
            )
            self.assertTrue(
                any(
                    action.startswith(
                        "REMOVE_VALID_BACKUP_AFTER_RECOVERY:"
                    )
                    for action in result.control_actions
                )
            )

    def test_applied_create_without_receipt_can_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.RECOVERABLE,
            )
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT,
            )
            self.assertTrue(
                result.control_actions[0].startswith(
                    "WRITE_COMMITTED_RECEIPT:"
                )
            )

    def test_applied_update_without_receipt_can_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            write_payload(context)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.FINALIZE_COMMIT,
            )
            self.assertIs(
                result.records[0].action,
                ArtifactTransactionRecoveryAction.FINALIZE_COMMIT,
            )

    def test_partial_create_can_rollback(self) -> None:
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
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.ROLLBACK,
            )
            self.assertIs(
                result.records[0].action,
                ArtifactTransactionRecoveryAction.DELETE_CREATED_DESTINATION,
            )
            self.assertIs(
                result.records[1].action,
                ArtifactTransactionRecoveryAction.NONE,
            )

    def test_partial_update_with_backup_can_rollback(self) -> None:
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
            context = make_context(
                Path(directory),
                specs,
                existing_content=b"old content\n",
            )
            write_valid_backup(context, 0)
            write_payload(context, 0)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.ROLLBACK,
            )
            self.assertIs(
                result.records[0].action,
                ArtifactTransactionRecoveryAction.RESTORE_BACKUP,
            )

    def test_finalize_disabled_falls_back_to_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_payload(context)
            policy = ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                allow_finalize_without_receipt=False,
            )
            result = make_reconciler(
                context,
                policy=policy,
            ).reconcile()
            self.assertIs(
                result.strategy,
                ArtifactTransactionRecoveryStrategy.ROLLBACK,
            )

    def test_cleanup_policy_can_keep_lock_action_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            write_matching_lock(context)
            policy = ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                plan_remove_residual_lock=False,
            )
            result = make_reconciler(
                context,
                policy=policy,
            ).reconcile()
            self.assertEqual(result.control_actions, ())


class ConflictedReconciliationTests(unittest.TestCase):
    def test_unknown_create_content_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            destination_for(context).write_bytes(b"unknown")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )
            self.assertIs(
                result.records[0].action,
                ArtifactTransactionRecoveryAction.INVESTIGATE,
            )

    def test_missing_update_destination_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            destination_for(context).unlink()
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_partial_update_without_backup_is_conflicted(self) -> None:
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
            write_payload(context, 0)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_mismatched_lock_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = lock_for(context)
            lock.parent.mkdir(parents=True)
            lock.write_text(
                canonical_json(
                    {
                        "transaction_id": "artifact-transaction:other",
                        "request_hash": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_invalid_lock_json_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock = lock_for(context)
            lock.parent.mkdir(parents=True)
            lock.write_text("{", encoding="utf-8")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_lock_directory_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            lock_for(context).mkdir(parents=True)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_corrupted_receipt_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            receipt = receipt_for(context)
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}", encoding="utf-8")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_receipt_directory_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            receipt_for(context).mkdir(parents=True)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_invalid_backup_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            write_valid_backup(context, content=b"wrong")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )
            self.assertIs(
                result.records[0].backup_state,
                ArtifactTransactionBackupState.INVALID,
            )

    def test_backup_directory_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE
                ),
            )
            backup_for(context).mkdir(parents=True)
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_committed_receipt_with_tampered_final_is_conflicted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context["application"].apply()
            destination_for(context).write_bytes(b"tampered")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_temporary_symlink_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            target = context["root"] / "target.txt"
            target.write_text("target", encoding="utf-8")
            link = context["root"] / "src/.elman-write-link.tmp"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_destination_symlink_is_conflicted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            target = context["root"] / "target.txt"
            target.write_text("target", encoding="utf-8")
            destination = destination_for(context)
            try:
                destination.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = make_reconciler(context).reconcile()
            self.assertIs(
                result.status,
                ArtifactTransactionReconciliationStatus.CONFLICTED,
            )

    def test_temporary_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            parent = context["root"] / "src"
            (parent / ".elman-write-one.tmp").write_bytes(b"1")
            (parent / ".elman-write-two.tmp").write_bytes(b"2")
            policy = ArtifactTransactionReconciliationPolicy(
                policy_id="policy:transaction-reconciliation-001",
                max_temporary_entries=1,
            )
            with self.assertRaises(
                ArtifactTransactionReconciliationError
            ):
                make_reconciler(
                    context,
                    policy=policy,
                ).reconcile()


class ReconciliationResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            restored = (
                ArtifactTransactionReconciliationResult.from_json(
                    original.to_json()
                )
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
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

    def test_control_entry_hash_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = make_reconciler(
                make_context(Path(directory))
            ).reconcile().control_entries[0]
            entry.verify_hash()

    def test_record_hash_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = make_reconciler(
                make_context(Path(directory))
            ).reconcile().records[0]
            record.verify_hash()

    def test_tampered_control_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            data = result.to_dict()
            data["control_entries"][0]["reason"] = "changed"
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationResult.from_dict(
                    data
                )

    def test_tampered_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            data = result.to_dict()
            data["records"][0]["destination_path"] = "src/other.txt"
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationResult.from_dict(
                    data
                )

    def test_tampered_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            data = result.to_dict()
            data["before_count"] = 0
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationResult.from_dict(
                    data
                )

    def test_tampered_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            data = result.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationResult.from_dict(
                    data
                )

    def test_missing_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactTransactionReconciliationIntegrityError
            ):
                ArtifactTransactionReconciliationResult.from_dict(
                    data
                )

    def test_result_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_reconciler(
                make_context(Path(directory))
            ).reconcile()
            with self.assertRaises(FrozenInstanceError):
                result.status = (  # type: ignore[misc]
                    ArtifactTransactionReconciliationStatus.CONFLICTED
                )


if __name__ == "__main__":
    unittest.main()
