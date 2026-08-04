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
    ArtifactApplicationDecision,
    ArtifactApplicationPolicy,
    ArtifactApplicationRequest,
    build_artifact_application_plan,
)
from elman_os.artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerification,
    ArtifactPayloadVerificationPolicy,
    ArtifactPayloadVerificationRequest,
    ArtifactPayloadVerificationStatus,
)
from elman_os.artifact_workspace_preflight import (
    ArtifactWorkspaceEntryType,
    ArtifactWorkspacePreflight,
    ArtifactWorkspacePreflightError,
    ArtifactWorkspacePreflightIntegrityError,
    ArtifactWorkspacePreflightPolicy,
    ArtifactWorkspacePreflightRequest,
    ArtifactWorkspacePreflightResult,
    ArtifactWorkspacePreflightStatus,
    ArtifactWorkspaceRecordDecision,
)


VALIDATED = "2026-08-04T04:00:00Z"
PLANNED = "2026-08-04T04:10:00Z"
VERIFIED = "2026-08-04T04:20:00Z"
INSPECTED = "2026-08-04T04:30:00Z"


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


def make_plan_and_verification(
    specifications=None,
):
    specs = specifications or default_specs()
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
    return plan, verification, specs


def make_policy(
    **overrides: object,
) -> ArtifactWorkspacePreflightPolicy:
    return ArtifactWorkspacePreflightPolicy(
        policy_id="policy:workspace-preflight-001",
        **overrides,
    )


def prepare_create_parents(
    root: Path,
    specifications,
) -> None:
    for path, _, _, _, operation in specifications:
        if operation is ArtifactOperation.CREATE:
            (root / Path(path).parent).mkdir(
                parents=True,
                exist_ok=True,
            )


def prepare_update_files(
    root: Path,
    specifications,
    *,
    existing_content: bytes = b"old content\n",
) -> None:
    for path, _, _, _, operation in specifications:
        if operation is ArtifactOperation.UPDATE:
            destination = root / path
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            destination.write_bytes(existing_content)


def make_preflight(
    root: Path,
    *,
    specifications=None,
    policy=None,
    prepare=True,
):
    plan, verification, specs = (
        make_plan_and_verification(specifications)
    )
    if prepare:
        prepare_create_parents(root, specs)
        prepare_update_files(root, specs)
    effective_policy = policy or make_policy()
    request = ArtifactWorkspacePreflightRequest.from_sources(
        plan,
        verification,
        effective_policy,
        workspace_root=root,
        requested_by="ELMAN_NEXUS",
        requested_at=INSPECTED,
    )
    return ArtifactWorkspacePreflight(
        request,
        plan,
        verification,
        effective_policy,
    ), plan, verification, specs


def snapshot_tree(root: Path) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            result[relative] = path.read_bytes()
        else:
            result[relative] = None
    return result


class WorkspacePreflightPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        self.assertEqual(
            make_policy().policy_hash,
            make_policy().policy_hash,
        )

    def test_policy_json_round_trip(self) -> None:
        original = make_policy(max_operations=8)

        restored = ArtifactWorkspacePreflightPolicy.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        self.assertEqual(restored.policy_hash, original.policy_hash)

    def test_policy_rejects_zero_max_operations(self) -> None:
        with self.assertRaises(ArtifactWorkspacePreflightError):
            make_policy(max_operations=0)

    def test_policy_rejects_zero_size_limit(self) -> None:
        with self.assertRaises(ArtifactWorkspacePreflightError):
            make_policy(max_existing_file_bytes=0)

    def test_policy_rejects_review_threshold_above_maximum(self) -> None:
        with self.assertRaises(ArtifactWorkspacePreflightError):
            make_policy(
                max_existing_file_bytes=10,
                review_existing_file_bytes=11,
            )

    def test_policy_rejects_non_boolean_option(self) -> None:
        with self.assertRaises(ArtifactWorkspacePreflightError):
            make_policy(reject_symlinks="yes")

    def test_policy_rejects_unknown_classification(self) -> None:
        with self.assertRaises(ArtifactWorkspacePreflightError):
            make_policy(
                review_classifications=("unknown",)
            )

    def test_policy_normalizes_classifications(self) -> None:
        policy = make_policy(
            review_classifications=(
                "source",
                "documentation",
                "source",
            )
        )

        self.assertEqual(
            policy.review_classifications,
            ("documentation", "source"),
        )


class WorkspacePreflightRequestTests(unittest.TestCase):
    def test_request_captures_source_hashes_and_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, verification, _ = (
                make_plan_and_verification()
            )
            policy = make_policy()

            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                policy,
                workspace_root=root,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )

            self.assertEqual(
                request.application_plan_hash,
                plan.plan_hash,
            )
            self.assertEqual(
                request.verification_result_hash,
                verification.result_hash,
            )
            self.assertEqual(
                request.workspace_root,
                root.resolve().as_posix(),
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, verification, _ = (
                make_plan_and_verification()
            )
            policy = make_policy()

            first = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                policy,
                workspace_root=root,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )
            second = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                policy,
                workspace_root=root,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )

            self.assertEqual(first.preflight_id, second.preflight_id)
            self.assertEqual(first.request_hash, second.request_hash)

    def test_request_accepts_explicit_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, verification, _ = (
                make_plan_and_verification()
            )

            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                make_policy(),
                workspace_root=directory,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
                preflight_id="workspace-preflight:operator-001",
            )

            self.assertEqual(
                request.preflight_id,
                "workspace-preflight:operator-001",
            )

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, verification, _ = (
                make_plan_and_verification()
            )
            original = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                make_policy(),
                workspace_root=directory,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )

            restored = ArtifactWorkspacePreflightRequest.from_json(
                original.to_json()
            )

            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, verification, _ = (
                make_plan_and_verification()
            )
            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                make_policy(),
                workspace_root=directory,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightRequest.from_dict(data)

    def test_request_rejects_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            plan, verification, _ = (
                make_plan_and_verification()
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflightRequest.from_sources(
                    plan,
                    verification,
                    make_policy(),
                    workspace_root=missing,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INSPECTED,
                )

    def test_request_rejects_file_as_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "file.txt"
            file_path.write_text("x", encoding="utf-8")
            plan, verification, _ = (
                make_plan_and_verification()
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflightRequest.from_sources(
                    plan,
                    verification,
                    make_policy(),
                    workspace_root=file_path,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INSPECTED,
                )

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, verification, _ = (
                make_plan_and_verification()
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflightRequest.from_sources(
                    plan,
                    verification,
                    make_policy(),
                    workspace_root=directory,
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
            plan, verification, _ = (
                make_plan_and_verification()
            )

            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                make_policy(),
                workspace_root=directory,
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=UTC,
                ),
            )

            self.assertEqual(
                request.requested_at,
                "2026-08-04T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, verification, _ = (
                make_plan_and_verification()
            )
            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                make_policy(),
                workspace_root=directory,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )

            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class WorkspacePreflightConstructionTests(unittest.TestCase):
    def test_preflight_accepts_matching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preflight, _, _, _ = make_preflight(
                Path(directory)
            )

            self.assertEqual(
                preflight.request.application_id,
                preflight.application_plan.application_id,
            )

    def test_preflight_rejects_other_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, verification, specs = (
                make_plan_and_verification()
            )
            prepare_create_parents(root, specs)
            first = make_policy()
            request = ArtifactWorkspacePreflightRequest.from_sources(
                plan,
                verification,
                first,
                workspace_root=root,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )
            second = ArtifactWorkspacePreflightPolicy(
                policy_id="policy:other",
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflight(
                    request,
                    plan,
                    verification,
                    second,
                )

    def test_preflight_rejects_other_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, verification, specs = (
                make_plan_and_verification()
            )
            prepare_create_parents(root, specs)
            policy = make_policy()
            request = ArtifactWorkspacePreflightRequest.from_sources(
                first,
                verification,
                policy,
                workspace_root=root,
                requested_by="ELMAN_NEXUS",
                requested_at=INSPECTED,
            )
            second, _, _ = make_plan_and_verification(
                default_specs(path="docs/other.txt")
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflight(
                    request,
                    second,
                    verification,
                    policy,
                )


class WorkspaceCreatePreflightTests(unittest.TestCase):
    def test_absent_create_destination_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.READY,
            )
            self.assertEqual(result.ready_count, 1)

    def test_create_snapshot_marks_destination_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            entry = result.snapshot[0]

            self.assertIs(
                entry.entry_type,
                ArtifactWorkspaceEntryType.ABSENT,
            )
            self.assertFalse(entry.destination_exists)
            self.assertIsNone(entry.existing_sha256)

    def test_existing_create_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight, _, _, _ = make_preflight(root)
            (root / "src/generated.txt").write_text(
                "already here",
                encoding="utf-8",
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_missing_create_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight, _, _, _ = make_preflight(
                root,
                prepare=False,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_missing_parent_can_be_allowed_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = make_policy(
                require_existing_parent_for_create=False,
                require_writable_parent=False,
            )
            preflight, _, _, _ = make_preflight(
                root,
                policy=policy,
                prepare=False,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.READY,
            )

    def test_non_writable_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight, _, _, _ = make_preflight(root)

            with patch(
                "elman_os.artifact_workspace_preflight.os.access",
                return_value=False,
            ):
                result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_case_conflicting_create_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(path="src/file.txt")
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
            )
            (root / "src/File.txt").write_text(
                "case conflict",
                encoding="utf-8",
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )
            self.assertTrue(
                result.snapshot[0].case_conflicts
                or result.snapshot[0].destination_exists
            )


class WorkspaceUpdatePreflightTests(unittest.TestCase):
    def test_existing_regular_update_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            result = make_preflight(
                Path(directory),
                specifications=specs,
            )[0].inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.READY,
            )

    def test_update_snapshot_hashes_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            result = make_preflight(
                root,
                specifications=specs,
            )[0].inspect()
            entry = result.snapshot[0]

            self.assertEqual(
                entry.existing_sha256,
                sha256(b"old content\n"),
            )
            self.assertEqual(
                entry.existing_size_bytes,
                len(b"old content\n"),
            )

    def test_missing_update_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
                prepare=False,
            )
            (root / "src").mkdir()

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_directory_update_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
                prepare=False,
            )
            (root / "src/generated.txt").mkdir(
                parents=True,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_large_existing_update_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            policy = make_policy(
                max_existing_file_bytes=5,
                review_existing_file_bytes=4,
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
                policy=policy,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_medium_existing_update_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            policy = make_policy(
                max_existing_file_bytes=100,
                review_existing_file_bytes=5,
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
                policy=policy,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REQUIRES_REVIEW,
            )
            self.assertEqual(result.review_count, 1)

    def test_update_snapshot_contains_rollback_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            result = make_preflight(
                Path(directory),
                specifications=specs,
            )[0].inspect()
            entry = result.snapshot[0]

            self.assertIsNotNone(entry.rollback_path)
            self.assertTrue(entry.rollback_available)

    def test_rollback_file_prefix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
            )
            (root / ".elman-os").write_text(
                "not a directory",
                encoding="utf-8",
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )


class WorkspacePreflightSecurityTests(unittest.TestCase):
    def test_operation_count_limit_is_rejected(self) -> None:
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
            result = make_preflight(
                Path(directory),
                specifications=specs,
                policy=make_policy(max_operations=1),
            )[0].inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_review_classification_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = default_specs(
                classification=(
                    ArtifactClassification.DOCUMENTATION
                )
            )
            result = make_preflight(
                Path(directory),
                specifications=specs,
                policy=make_policy(
                    review_classifications=("documentation",)
                ),
            )[0].inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REQUIRES_REVIEW,
            )

    def test_inspection_does_not_modify_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
            )
            before = snapshot_tree(root)

            preflight.inspect()

            self.assertEqual(snapshot_tree(root), before)

    def test_result_is_deterministic_for_unchanged_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preflight, _, _, _ = make_preflight(
                Path(directory)
            )

            first = preflight.inspect()
            second = preflight.inspect()

            self.assertEqual(first.to_json(), second.to_json())

    def test_workspace_change_changes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = default_specs(
                operation=ArtifactOperation.UPDATE
            )
            preflight, _, _, _ = make_preflight(
                root,
                specifications=specs,
            )
            first = preflight.inspect()
            (root / "src/generated.txt").write_bytes(
                b"changed existing content"
            )
            second = preflight.inspect()

            self.assertNotEqual(
                first.snapshot_hash,
                second.snapshot_hash,
            )

    def test_symlink_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("target", encoding="utf-8")
            (root / "src").mkdir()
            link = root / "src/generated.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            preflight, _, _, _ = make_preflight(
                root,
                prepare=False,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )
            self.assertTrue(result.snapshot[0].symlink_paths)

    def test_symlink_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real"
            target.mkdir()
            link = root / "src"
            try:
                link.symlink_to(
                    target,
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            preflight, _, _, _ = make_preflight(
                root,
                prepare=False,
            )

            result = preflight.inspect()

            self.assertIs(
                result.status,
                ArtifactWorkspacePreflightStatus.REJECTED,
            )

    def test_symlink_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            try:
                link.symlink_to(
                    real,
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            plan, verification, _ = (
                make_plan_and_verification()
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightError
            ):
                ArtifactWorkspacePreflightRequest.from_sources(
                    plan,
                    verification,
                    make_policy(),
                    workspace_root=link,
                    requested_by="ELMAN_NEXUS",
                    requested_at=INSPECTED,
                )


class WorkspacePreflightResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_preflight(
                Path(directory)
            )[0].inspect()

            restored = ArtifactWorkspacePreflightResult.from_json(
                original.to_json()
            )

            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
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

    def test_tampered_snapshot_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["snapshot"][0]["parent_writable"] = False

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_tampered_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["records"][0]["destination_path"] = (
                "src/other.txt"
            )

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_tampered_snapshot_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["snapshot_hash"] = "f" * 64

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_tampered_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["ready_count"] = 0

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_tampered_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["status"] = "rejected"

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            data["agent_id"] = "ELMAN_OTHER"

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()
            data = result.to_dict()
            del data["result_hash"]

            with self.assertRaises(
                ArtifactWorkspacePreflightIntegrityError
            ):
                ArtifactWorkspacePreflightResult.from_dict(data)

    def test_snapshot_entry_hash_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = make_preflight(
                Path(directory)
            )[0].inspect().snapshot[0]
            entry.verify_hash()

    def test_result_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_preflight(
                Path(directory)
            )[0].inspect()

            with self.assertRaises(FrozenInstanceError):
                result.status = (  # type: ignore[misc]
                    ArtifactWorkspacePreflightStatus.REJECTED
                )


if __name__ == "__main__":
    unittest.main()
