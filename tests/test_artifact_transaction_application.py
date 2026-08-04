from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
import os
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
    ArtifactTransactionIntegrityError,
    ArtifactTransactionLockError,
    ArtifactTransactionOperationResult,
    ArtifactTransactionOperationStatus,
    ArtifactTransactionPolicy,
    ArtifactTransactionRequest,
    ArtifactTransactionResult,
    ArtifactTransactionStatus,
)
from elman_os.artifact_workspace_preflight import (
    ArtifactWorkspacePreflight,
    ArtifactWorkspacePreflightPolicy,
    ArtifactWorkspacePreflightRequest,
)


VALIDATED = "2026-08-04T05:00:00Z"
PLANNED = "2026-08-04T05:10:00Z"
VERIFIED = "2026-08-04T05:20:00Z"
INSPECTED = "2026-08-04T05:30:00Z"
APPLIED = "2026-08-04T05:40:00Z"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def make_validation_record(
    index: int,
    path: str,
    content: bytes,
    *,
    media_type: str = "text/plain",
    classification: ArtifactClassification = (
        ArtifactClassification.SOURCE
    ),
    operation: ArtifactOperation = ArtifactOperation.CREATE,
) -> ArtifactValidationRecord:
    return ArtifactValidationRecord(
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


def make_validation_result(
    specifications: tuple[
        tuple[
            str,
            bytes,
            str,
            ArtifactClassification,
            ArtifactOperation,
        ],
        ...,
    ],
) -> AgentOutputValidationResult:
    records = tuple(
        make_validation_record(
            index,
            path,
            content,
            media_type=media_type,
            classification=classification,
            operation=operation,
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


def default_specs(
    *,
    operation: ArtifactOperation = ArtifactOperation.CREATE,
    path: str = "src/generated.txt",
    content: bytes = b"new payload\n",
    classification: ArtifactClassification = (
        ArtifactClassification.SOURCE
    ),
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
            classification,
            operation,
        ),
    )


def prepare_workspace(
    root: Path,
    specifications,
    *,
    existing_content: bytes = b"old content\n",
) -> None:
    for path, _, _, _, operation in specifications:
        destination = root / path
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        if operation is ArtifactOperation.UPDATE:
            destination.write_bytes(existing_content)


def make_sources(
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
    payload_policy = ArtifactPayloadVerificationPolicy(
        policy_id="policy:payload-verification-001",
        review_media_types=(),
    )
    payload_request = (
        ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            payload_policy,
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
    )
    verification = ArtifactPayloadVerification(
        payload_request,
        plan,
        payloads,
        payload_policy,
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

    policy = transaction_policy or ArtifactTransactionPolicy(
        policy_id="policy:transaction-001",
    )
    request = ArtifactTransactionRequest.from_sources(
        plan,
        verification,
        preflight,
        policy,
        requested_by="ELMAN_NEXUS",
        requested_at=APPLIED,
    )
    application = ArtifactTransactionApplication(
        request,
        plan,
        verification,
        preflight,
        policy,
    )
    return application, plan, verification, preflight, specs


def read_receipt(
    application: ArtifactTransactionApplication,
) -> ArtifactTransactionResult:
    receipt = (
        Path(application.request.workspace_root)
        / application.policy.receipt_relative_path(
            application.request.transaction_id
        )
    )
    return ArtifactTransactionResult.from_json(
        receipt.read_text(encoding="utf-8").strip()
    )


def visible_temp_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.name.startswith(
                (".elman-write-", ".elman-backup-", ".elman-receipt-")
            )
        )
    )


class TransactionPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        first = ArtifactTransactionPolicy(
            policy_id="policy:transaction-001",
        )
        second = ArtifactTransactionPolicy(
            policy_id="policy:transaction-001",
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self) -> None:
        original = ArtifactTransactionPolicy(
            policy_id="policy:transaction-001",
            max_operations=8,
        )
        restored = ArtifactTransactionPolicy.from_json(
            original.to_json()
        )
        self.assertEqual(restored, original)

    def test_policy_rejects_zero_operation_limit(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                max_operations=0,
            )

    def test_policy_rejects_zero_payload_limit(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                max_total_payload_bytes=0,
            )

    def test_policy_rejects_non_boolean_fsync(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                fsync_files="yes",
            )

    def test_policy_rejects_nested_lock_name(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                lock_name="locks/current",
            )

    def test_policy_rejects_nested_receipt_directory(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                receipt_directory="transactions/current",
            )

    def test_policy_receipt_path_is_portable(self) -> None:
        policy = ArtifactTransactionPolicy(
            policy_id="policy:transaction-001",
        )
        path = policy.receipt_relative_path(
            "artifact-transaction:" + "a" * 64
        )
        self.assertTrue(path.startswith(".elman-os/transactions/"))
        self.assertTrue(path.endswith(".json"))

    def test_policy_control_root_is_portable(self) -> None:
        with self.assertRaises(ArtifactTransactionError):
            ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                control_root="../control",
            )


class TransactionRequestTests(unittest.TestCase):
    def test_request_captures_all_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            request = application.request
            self.assertEqual(
                request.application_plan_hash,
                plan.plan_hash,
            )
            self.assertEqual(
                request.verification_result_hash,
                verification.result_hash,
            )
            self.assertEqual(
                request.preflight_result_hash,
                preflight.result_hash,
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_sources(root)[0].request
            second = make_sources(root)[0].request
            self.assertEqual(
                first.transaction_id,
                second.transaction_id,
            )

    def test_request_id_is_independent_of_requested_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            second = ArtifactTransactionRequest.from_sources(
                plan,
                verification,
                preflight,
                application.policy,
                requested_by="ELMAN_NEXUS",
                requested_at="2026-08-04T06:00:00Z",
            )
            self.assertEqual(
                application.request.transaction_id,
                second.transaction_id,
            )
            self.assertNotEqual(
                application.request.request_hash,
                second.request_hash,
            )

    def test_request_accepts_explicit_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            request = ArtifactTransactionRequest.from_sources(
                plan,
                verification,
                preflight,
                application.policy,
                requested_by="ELMAN_NEXUS",
                requested_at=APPLIED,
                transaction_id="artifact-transaction:operator-001",
            )
            self.assertEqual(
                request.transaction_id,
                "artifact-transaction:operator-001",
            )

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_sources(Path(directory))[0].request
            restored = ArtifactTransactionRequest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_sources(Path(directory))[0].request
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionRequest.from_dict(data)

    def test_request_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_sources(Path(directory))[0].request
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            with self.assertRaises(ArtifactTransactionError):
                ArtifactTransactionRequest.from_sources(
                    plan,
                    verification,
                    preflight,
                    application.policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=datetime(
                        2026,
                        8,
                        4,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            request = ArtifactTransactionRequest.from_sources(
                plan,
                verification,
                preflight,
                application.policy,
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(2026, 8, 4, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-04T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_sources(Path(directory))[0].request
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class TransactionConstructionTests(unittest.TestCase):
    def test_application_accepts_matching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = make_sources(Path(directory))[0]
            self.assertEqual(
                application.request.application_id,
                application.application_plan.application_id,
            )

    def test_application_rejects_other_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application, plan, verification, preflight, _ = (
                make_sources(Path(directory))
            )
            other = ArtifactTransactionPolicy(
                policy_id="policy:other",
            )
            with self.assertRaises(ArtifactTransactionError):
                ArtifactTransactionApplication(
                    application.request,
                    plan,
                    verification,
                    preflight,
                    other,
                )

    def test_application_rejects_other_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application, _, verification, preflight, _ = (
                make_sources(root)
            )
            other_plan = make_sources(
                root,
                default_specs(path="docs/other.txt"),
            )[1]
            with self.assertRaises(ArtifactTransactionError):
                ArtifactTransactionApplication(
                    application.request,
                    other_plan,
                    verification,
                    preflight,
                    application.policy,
                )

    def test_application_rejects_control_root_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactTransactionError):
                make_sources(
                    Path(directory),
                    default_specs(
                        path=".elman-os/generated.txt"
                    ),
                )

    def test_application_rejects_payload_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                max_total_payload_bytes=1,
            )
            with self.assertRaises(ArtifactTransactionError):
                make_sources(
                    Path(directory),
                    transaction_policy=policy,
                )

    def test_application_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = make_sources(Path(directory))[0]
            with self.assertRaises(FrozenInstanceError):
                application.policy = None  # type: ignore[misc]


class TransactionCreateTests(unittest.TestCase):
    def test_single_create_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.COMMITTED,
            )
            self.assertEqual(
                (root / "src/generated.txt").read_bytes(),
                b"new payload\n",
            )

    def test_create_uses_no_overwrite_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            (root / "src/generated.txt").write_text(
                "appeared",
                encoding="utf-8",
            )
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.FAILED,
            )
            self.assertEqual(
                (root / "src/generated.txt").read_text(
                    encoding="utf-8"
                ),
                "appeared",
            )

    def test_create_writes_committed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = make_sources(Path(directory))[0]
            result = application.apply()
            receipt = read_receipt(application)
            self.assertEqual(receipt, result)
            receipt.verify_hash()

    def test_create_removes_lock_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            application.apply()
            self.assertFalse(
                (
                    root
                    / application.policy.lock_relative_path
                ).exists()
            )

    def test_create_cleans_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            application.apply()
            self.assertEqual(visible_temp_files(root), ())

    def test_multiple_creates_commit_in_plan_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = (
                (
                    "src/b.txt",
                    b"b",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
                (
                    "src/a.txt",
                    b"a",
                    "text/plain",
                    ArtifactClassification.SOURCE,
                    ArtifactOperation.CREATE,
                ),
            )
            result = make_sources(
                Path(directory),
                specs,
            )[0].apply()
            self.assertEqual(
                tuple(item.destination_path for item in result.operations),
                ("src/a.txt", "src/b.txt"),
            )

    def test_create_result_is_deterministic_for_receipt_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = make_sources(Path(directory))[0]
            first = application.apply()
            second = application.apply()
            self.assertEqual(first, second)
            self.assertEqual(first.to_json(), second.to_json())

    def test_create_receipt_replay_performs_no_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            application.apply()
            destination = root / "src/generated.txt"
            before = destination.stat().st_mtime_ns
            application.apply()
            self.assertEqual(
                destination.stat().st_mtime_ns,
                before,
            )


class TransactionUpdateTests(unittest.TestCase):
    def test_single_update_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application, plan, _, _, _ = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.COMMITTED,
            )
            self.assertEqual(
                (root / "src/generated.txt").read_bytes(),
                b"new payload\n",
            )
            self.assertIsNotNone(
                plan.operations[0].backup_path
            )

    def test_update_retains_verified_backup_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application, plan, _, _, _ = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
            application.apply()
            backup = root / (plan.operations[0].backup_path or "")
            self.assertTrue(backup.is_file())
            self.assertEqual(
                backup.read_bytes(),
                b"old content\n",
            )

    def test_update_can_remove_backup_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = ArtifactTransactionPolicy(
                policy_id="policy:transaction-001",
                retain_backups_on_success=False,
            )
            application, plan, _, _, _ = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
                transaction_policy=policy,
            )
            result = application.apply()
            backup = root / (plan.operations[0].backup_path or "")
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.COMMITTED,
            )
            self.assertFalse(backup.exists())

    def test_update_result_records_before_and_after_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(
                Path(directory),
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )[0].apply()
            operation = result.operations[0]
            self.assertEqual(
                operation.before_sha256,
                sha256(b"old content\n"),
            )
            self.assertEqual(
                operation.after_sha256,
                sha256(b"new payload\n"),
            )

    def test_stale_update_is_failed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )[0]
            (root / "src/generated.txt").write_bytes(
                b"changed after preflight"
            )
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.FAILED,
            )
            self.assertEqual(
                (root / "src/generated.txt").read_bytes(),
                b"changed after preflight",
            )

    def test_existing_backup_is_failed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application, plan, _, _, _ = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
            backup = root / (plan.operations[0].backup_path or "")
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"existing backup")
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.FAILED,
            )
            self.assertEqual(
                backup.read_bytes(),
                b"existing backup",
            )

    def test_update_cleans_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )[0]
            application.apply()
            self.assertEqual(visible_temp_files(root), ())

    def test_update_receipt_replay_verifies_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(
                root,
                default_specs(
                    operation=ArtifactOperation.UPDATE,
                ),
            )[0]
            application.apply()
            (root / "src/generated.txt").write_bytes(
                b"tampered after commit"
            )
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                application.apply()


class TransactionLockAndSecurityTests(unittest.TestCase):
    def test_existing_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            lock = root / application.policy.lock_relative_path
            lock.parent.mkdir(parents=True)
            lock.write_text("held", encoding="utf-8")
            with self.assertRaises(ArtifactTransactionLockError):
                application.apply()

    def test_symlink_destination_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            target = root / "target.txt"
            target.write_text("target", encoding="utf-8")
            link = root / "src/generated.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.FAILED,
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "target",
            )

    def test_corrupted_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            application.apply()
            receipt = (
                root
                / application.policy.receipt_relative_path(
                    application.request.transaction_id
                )
            )
            receipt.write_text("{}", encoding="utf-8")
            with self.assertRaises(
                ArtifactTransactionError
            ):
                application.apply()

    def test_receipt_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            receipt = (
                root
                / application.policy.receipt_relative_path(
                    application.request.transaction_id
                )
            )
            receipt.parent.mkdir(parents=True)
            target = root / "receipt-target.json"
            target.write_text("{}", encoding="utf-8")
            try:
                receipt.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                application.apply()

    def test_result_contains_no_execution_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            payload = result.to_json()
            self.assertNotIn("subprocess", payload)
            self.assertNotIn("execute_artifact", payload)
            self.assertNotIn("network", payload)


class TransactionRollbackTests(unittest.TestCase):
    def test_second_create_failure_rolls_back_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            application = make_sources(root, specs)[0]
            original = ArtifactTransactionApplication._commit_create

            def flaky(self, temp_path, destination):
                if destination.name == "b.txt":
                    raise ArtifactTransactionError(
                        "injected second create failure"
                    )
                return original(self, temp_path, destination)

            with patch.object(
                ArtifactTransactionApplication,
                "_commit_create",
                new=flaky,
            ):
                result = application.apply()

            self.assertIs(
                result.status,
                ArtifactTransactionStatus.ROLLED_BACK,
            )
            self.assertFalse((root / "src/a.txt").exists())
            self.assertFalse((root / "src/b.txt").exists())
            self.assertIs(
                result.operations[0].status,
                ArtifactTransactionOperationStatus.ROLLED_BACK,
            )

    def test_update_is_restored_when_later_create_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
                    ArtifactOperation.CREATE,
                ),
            )
            application = make_sources(
                root,
                specs,
                existing_content=b"old-a",
            )[0]
            original = ArtifactTransactionApplication._commit_create

            def fail_create(self, temp_path, destination):
                raise ArtifactTransactionError(
                    "injected create failure"
                )

            with patch.object(
                ArtifactTransactionApplication,
                "_commit_create",
                new=fail_create,
            ):
                result = application.apply()

            self.assertIs(
                result.status,
                ArtifactTransactionStatus.ROLLED_BACK,
            )
            self.assertEqual(
                (root / "src/a.txt").read_bytes(),
                b"old-a",
            )
            self.assertFalse((root / "src/b.txt").exists())
            self.assertIsNot(
                original,
                fail_create,
            )

    def test_receipt_failure_rolls_back_committed_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = make_sources(root)[0]
            with patch.object(
                ArtifactTransactionApplication,
                "_write_receipt",
                side_effect=ArtifactTransactionError(
                    "injected receipt failure"
                ),
            ):
                result = application.apply()
            self.assertIs(
                result.status,
                ArtifactTransactionStatus.ROLLED_BACK,
            )
            self.assertFalse(
                (root / "src/generated.txt").exists()
            )

    def test_external_change_causes_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            application = make_sources(root, specs)[0]
            original = ArtifactTransactionApplication._commit_create

            def mutate_then_fail(self, temp_path, destination):
                if destination.name == "b.txt":
                    (root / "src/a.txt").write_bytes(
                        b"external mutation"
                    )
                    raise ArtifactTransactionError(
                        "injected failure after external mutation"
                    )
                return original(self, temp_path, destination)

            with patch.object(
                ArtifactTransactionApplication,
                "_commit_create",
                new=mutate_then_fail,
            ):
                result = application.apply()

            self.assertIs(
                result.status,
                ArtifactTransactionStatus.FAILED,
            )
            self.assertEqual(
                (root / "src/a.txt").read_bytes(),
                b"external mutation",
            )


class TransactionResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_sources(Path(directory))[0].apply()
            restored = ArtifactTransactionResult.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
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

    def test_operation_result_hash_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operation = make_sources(
                Path(directory)
            )[0].apply().operations[0]
            operation.verify_hash()

    def test_tampered_operation_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            data = result.to_dict()
            data["operations"][0]["bytes_written"] = 999
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionResult.from_dict(data)

    def test_tampered_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            data = result.to_dict()
            data["committed_count"] = 0
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            data = result.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactTransactionIntegrityError
            ):
                ArtifactTransactionResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_sources(Path(directory))[0].apply()
            with self.assertRaises(FrozenInstanceError):
                result.status = (  # type: ignore[misc]
                    ArtifactTransactionStatus.FAILED
                )

    def test_operation_result_is_frozen(self) -> None:
        result = ArtifactTransactionOperationResult(
            sequence=1,
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            operation=ArtifactOperation.CREATE,
            status=ArtifactTransactionOperationStatus.SKIPPED,
            payload_sha256="a" * 64,
            before_sha256=None,
            after_sha256=None,
            backup_path=None,
            bytes_written=0,
            reason="SKIPPED: test",
        )
        with self.assertRaises(FrozenInstanceError):
            result.reason = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
