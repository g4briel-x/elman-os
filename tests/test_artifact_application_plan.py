from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

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
    ArtifactApplicationOperation,
    ArtifactApplicationPlan,
    ArtifactApplicationPlanError,
    ArtifactApplicationPlanIntegrityError,
    ArtifactApplicationPolicy,
    ArtifactApplicationRequest,
    ArtifactRollbackAction,
    ArtifactRollbackEntry,
    build_artifact_application_plan,
)


VALIDATED = "2026-08-04T02:00:00Z"
REQUESTED = "2026-08-04T02:10:00Z"


def make_record(
    index: int = 0,
    *,
    path: str | None = "src/generated.py",
    decision: ArtifactValidationDecision = (
        ArtifactValidationDecision.ACCEPTED
    ),
    classification: ArtifactClassification = (
        ArtifactClassification.SOURCE
    ),
    operation: ArtifactOperation | None = ArtifactOperation.CREATE,
    sha256: str | None = "a" * 64,
    size_bytes: int | None = 120,
    media_type: str | None = "text/x-python",
    reasons: tuple[str, ...] | None = None,
) -> ArtifactValidationRecord:
    effective_reasons = reasons
    if effective_reasons is None:
        effective_reasons = {
            ArtifactValidationDecision.ACCEPTED: (
                "ACCEPTED: artifact declaration satisfies policy",
            ),
            ArtifactValidationDecision.REJECTED: (
                "REJECTED: invalid declaration",
            ),
            ArtifactValidationDecision.REQUIRES_REVIEW: (
                "REVIEW: operator review required",
            ),
        }[decision]
    return ArtifactValidationRecord(
        index=index,
        path=path,
        decision=decision,
        classification=classification,
        operation=operation,
        sha256=sha256,
        size_bytes=size_bytes,
        media_type=media_type,
        reasons=effective_reasons,
    )


def make_validation_result(
    records: tuple[ArtifactValidationRecord, ...] | None = None,
    *,
    status: AgentOutputValidationStatus | None = None,
    top_level_reasons: tuple[str, ...] = (),
    validation_id: str = "output-validation:001",
) -> AgentOutputValidationResult:
    effective_records = records or (make_record(),)
    accepted = sum(
        item.decision is ArtifactValidationDecision.ACCEPTED
        for item in effective_records
    )
    review = sum(
        item.decision
        is ArtifactValidationDecision.REQUIRES_REVIEW
        for item in effective_records
    )
    rejected = sum(
        item.decision is ArtifactValidationDecision.REJECTED
        for item in effective_records
    )
    total = sum(
        item.size_bytes
        for item in effective_records
        if item.size_bytes is not None
    )
    effective_status = status
    if effective_status is None:
        effective_status = (
            AgentOutputValidationStatus.REJECTED
            if rejected
            or any(
                item.startswith("REJECTED:")
                for item in top_level_reasons
            )
            else AgentOutputValidationStatus.REQUIRES_REVIEW
            if review
            or any(
                item.startswith("REVIEW:")
                for item in top_level_reasons
            )
            else AgentOutputValidationStatus.ACCEPTED
        )

    return AgentOutputValidationResult(
        validation_id=validation_id,
        status=effective_status,
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
        records=effective_records,
        top_level_reasons=top_level_reasons,
        accepted_count=accepted,
        review_count=review,
        rejected_count=rejected,
        total_declared_bytes=total,
        validated_at=VALIDATED,
        plan_state_hash="5" * 64,
        journal_event_count=12,
        journal_head_hash="6" * 64,
        journal_hash="7" * 64,
    )


def make_policy(**overrides: object) -> ArtifactApplicationPolicy:
    return ArtifactApplicationPolicy(
        policy_id="policy:artifact-application-001",
        **overrides,
    )


def make_request(
    result: AgentOutputValidationResult,
    policy: ArtifactApplicationPolicy | None = None,
    *,
    approval_reference: str | None = None,
    requested_at: str | datetime = REQUESTED,
    application_id: str | None = None,
) -> ArtifactApplicationRequest:
    return ArtifactApplicationRequest.from_validation_result(
        result,
        policy or make_policy(),
        requested_by="ELMAN_NEXUS",
        requested_at=requested_at,
        approval_reference=approval_reference,
        application_id=application_id,
    )


def make_plan(
    result: AgentOutputValidationResult | None = None,
    policy: ArtifactApplicationPolicy | None = None,
    *,
    approval_reference: str | None = None,
) -> ArtifactApplicationPlan:
    effective_result = result or make_validation_result()
    effective_policy = policy or make_policy()
    request = make_request(
        effective_result,
        effective_policy,
        approval_reference=approval_reference,
    )
    return build_artifact_application_plan(
        request,
        effective_result,
        effective_policy,
    )


class ArtifactApplicationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        self.assertEqual(
            make_policy().policy_hash,
            make_policy().policy_hash,
        )

    def test_policy_json_round_trip(self) -> None:
        original = make_policy(max_operations=8)

        restored = ArtifactApplicationPolicy.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        self.assertEqual(restored.policy_hash, original.policy_hash)

    def test_policy_rejects_zero_max_operations(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(max_operations=0)

    def test_policy_rejects_non_boolean_update_approval(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(
                require_human_approval_for_updates="yes"
            )

    def test_policy_rejects_non_boolean_rollback(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(rollback_required="yes")

    def test_policy_rejects_absolute_rollback_root(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(rollback_root="/tmp/rollback")

    def test_policy_rejects_traversal_rollback_root(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(rollback_root="../rollback")

    def test_policy_rejects_empty_classifications(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_policy(allowed_classifications=())

    def test_policy_rejects_unknown_classification(self) -> None:
        with self.assertRaises(ValueError):
            make_policy(
                allowed_classifications=("unknown",)
            )


class ArtifactApplicationRequestTests(unittest.TestCase):
    def test_request_captures_validation_boundary(self) -> None:
        result = make_validation_result()
        policy = make_policy()

        request = make_request(result, policy)

        self.assertEqual(
            request.validation_result_hash,
            result.result_hash,
        )
        self.assertEqual(
            request.plan_state_hash,
            result.plan_state_hash,
        )
        self.assertEqual(
            request.journal_event_count,
            result.journal_event_count,
        )
        self.assertEqual(request.policy_hash, policy.policy_hash)

    def test_default_application_id_is_deterministic(self) -> None:
        result = make_validation_result()
        policy = make_policy()

        first = make_request(result, policy)
        second = make_request(result, policy)

        self.assertEqual(first.application_id, second.application_id)
        self.assertEqual(first.request_hash, second.request_hash)

    def test_explicit_application_id_is_supported(self) -> None:
        result = make_validation_result()

        request = make_request(
            result,
            application_id="artifact-application:operator-001",
        )

        self.assertEqual(
            request.application_id,
            "artifact-application:operator-001",
        )

    def test_approval_reference_is_preserved(self) -> None:
        request = make_request(
            make_validation_result(),
            approval_reference="approval:artifact-update-001",
        )

        self.assertEqual(
            request.approval_reference,
            "approval:artifact-update-001",
        )

    def test_request_json_round_trip(self) -> None:
        original = make_request(make_validation_result())

        restored = ArtifactApplicationRequest.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        request = make_request(make_validation_result())
        data = request.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationRequest.from_dict(data)

    def test_request_rejects_missing_hash(self) -> None:
        request = make_request(make_validation_result())
        data = request.to_dict()
        del data["request_hash"]

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        with self.assertRaises(ArtifactApplicationPlanError):
            make_request(
                make_validation_result(),
                requested_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_accepts_utc_datetime(self) -> None:
        request = make_request(
            make_validation_result(),
            requested_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(
            request.requested_at,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_request_is_frozen(self) -> None:
        request = make_request(make_validation_result())

        with self.assertRaises(FrozenInstanceError):
            request.plan_id = "plan:other"  # type: ignore[misc]


class ArtifactApplicationOperationTests(unittest.TestCase):
    def test_create_operation_contract(self) -> None:
        operation = make_plan().operations[0]

        self.assertIs(operation.operation, ArtifactOperation.CREATE)
        self.assertFalse(operation.requires_backup)
        self.assertIsNone(operation.backup_path)
        self.assertIs(
            operation.rollback_action,
            ArtifactRollbackAction.DELETE_CREATED,
        )
        self.assertEqual(
            operation.precondition,
            "destination-must-not-exist",
        )

    def test_update_operation_contract(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )

        operation = make_plan(
            result,
            approval_reference="approval:update-001",
        ).operations[0]

        self.assertTrue(operation.requires_backup)
        self.assertIsNotNone(operation.backup_path)
        self.assertIs(
            operation.rollback_action,
            ArtifactRollbackAction.RESTORE_BACKUP,
        )
        self.assertEqual(
            operation.precondition,
            "destination-must-exist-and-be-backed-up",
        )

    def test_operation_hash_verifies(self) -> None:
        operation = make_plan().operations[0]
        operation.verify_hash()

    def test_operation_round_trip(self) -> None:
        original = make_plan().operations[0]

        restored = ArtifactApplicationOperation.from_dict(
            original.to_dict()
        )

        self.assertEqual(restored, original)

    def test_create_cannot_require_backup(self) -> None:
        operation = make_plan().operations[0]

        with self.assertRaises(ArtifactApplicationPlanError):
            replace(
                operation,
                requires_backup=True,
                backup_path=".elman-os/rollback/x/file.py",
            )

    def test_update_requires_backup_path(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )
        operation = make_plan(
            result,
            approval_reference="approval:update-001",
        ).operations[0]

        with self.assertRaises(ArtifactApplicationPlanError):
            replace(operation, backup_path=None)

    def test_operation_rejects_traversal_destination(self) -> None:
        operation = make_plan().operations[0]

        with self.assertRaises(ArtifactApplicationPlanError):
            replace(
                operation,
                destination_path="../secret.txt",
            )

    def test_operation_rejects_invalid_media_type(self) -> None:
        operation = make_plan().operations[0]

        with self.assertRaises(ArtifactApplicationPlanError):
            replace(operation, media_type="Text/Python")

    def test_operation_is_frozen(self) -> None:
        operation = make_plan().operations[0]

        with self.assertRaises(FrozenInstanceError):
            operation.sequence = 2  # type: ignore[misc]


class ArtifactApplicationReadyPlanTests(unittest.TestCase):
    def test_single_create_is_ready(self) -> None:
        plan = make_plan()

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.READY,
        )
        self.assertTrue(plan.executable)
        self.assertEqual(plan.reasons, ())

    def test_multiple_operations_are_sorted_by_portable_path(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    0,
                    path="tests/test_z.py",
                    sha256="a" * 64,
                    classification=ArtifactClassification.TEST,
                ),
                make_record(
                    1,
                    path="docs/a.md",
                    sha256="b" * 64,
                    classification=(
                        ArtifactClassification.DOCUMENTATION
                    ),
                    media_type="text/markdown",
                ),
                make_record(
                    2,
                    path="src/m.py",
                    sha256="c" * 64,
                ),
            )
        )

        plan = make_plan(result)

        self.assertEqual(
            tuple(
                item.destination_path
                for item in plan.operations
            ),
            (
                "docs/a.md",
                "src/m.py",
                "tests/test_z.py",
            ),
        )
        self.assertEqual(
            tuple(item.sequence for item in plan.operations),
            (1, 2, 3),
        )

    def test_operation_identifiers_are_deterministic(self) -> None:
        first = make_plan()
        second = make_plan()

        self.assertEqual(
            first.operations[0].operation_id,
            second.operations[0].operation_id,
        )

    def test_create_rollback_manifest_deletes_created_file(self) -> None:
        entry = make_plan().rollback_manifest[0]

        self.assertIs(
            entry.action,
            ArtifactRollbackAction.DELETE_CREATED,
        )
        self.assertIsNone(entry.backup_path)

    def test_update_rollback_manifest_restores_backup(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )

        entry = make_plan(
            result,
            approval_reference="approval:update-001",
        ).rollback_manifest[0]

        self.assertIs(
            entry.action,
            ArtifactRollbackAction.RESTORE_BACKUP,
        )
        self.assertTrue(
            entry.backup_path.startswith(
                ".elman-os/rollback/"
            )
        )

    def test_manifest_covers_every_operation(self) -> None:
        plan = make_plan()

        self.assertEqual(
            len(plan.operations),
            len(plan.rollback_manifest),
        )
        self.assertEqual(
            plan.operations[0].destination_path,
            plan.rollback_manifest[0].destination_path,
        )

    def test_plan_preserves_validation_boundary(self) -> None:
        result = make_validation_result()
        plan = make_plan(result)

        self.assertEqual(
            plan.validation_result_hash,
            result.result_hash,
        )
        self.assertEqual(
            plan.plan_state_hash,
            result.plan_state_hash,
        )
        self.assertEqual(
            plan.journal_hash,
            result.journal_hash,
        )

    def test_builder_does_not_mutate_validation_result(self) -> None:
        result = make_validation_result()
        before = result.to_json()

        make_plan(result)

        self.assertEqual(result.to_json(), before)

    def test_plan_is_deterministic(self) -> None:
        result = make_validation_result()
        policy = make_policy()
        request = make_request(result, policy)

        first = build_artifact_application_plan(
            request,
            result,
            policy,
        )
        second = build_artifact_application_plan(
            request,
            result,
            policy,
        )

        self.assertEqual(first.to_json(), second.to_json())

    def test_plan_contains_no_workspace_content(self) -> None:
        plan = make_plan()
        payload = plan.to_json()

        self.assertNotIn("file_content", payload)
        self.assertNotIn("patch_content", payload)
        self.assertNotIn("command", payload)


class ArtifactApplicationApprovalTests(unittest.TestCase):
    def test_update_without_approval_requires_approval(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REQUIRES_APPROVAL,
        )
        self.assertFalse(plan.executable)
        self.assertTrue(plan.reasons)

    def test_update_with_approval_is_ready(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )

        plan = make_plan(
            result,
            approval_reference="approval:update-001",
        )

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.READY,
        )
        self.assertEqual(
            plan.operations[0].approval_reference,
            "approval:update-001",
        )

    def test_policy_can_disable_update_approval_requirement(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    operation=ArtifactOperation.UPDATE,
                ),
            )
        )
        policy = make_policy(
            require_human_approval_for_updates=False
        )

        plan = make_plan(result, policy)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.READY,
        )

    def test_create_does_not_inherit_update_approval(self) -> None:
        plan = make_plan(
            approval_reference="approval:unused-001"
        )

        self.assertIsNone(
            plan.operations[0].approval_reference
        )


class ArtifactApplicationRejectedPlanTests(unittest.TestCase):
    def test_review_result_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    decision=(
                        ArtifactValidationDecision.REQUIRES_REVIEW
                    ),
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )
        self.assertEqual(plan.operations, ())

    def test_rejected_result_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    decision=ArtifactValidationDecision.REJECTED,
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_incomplete_record_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(path=None),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_disallowed_classification_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    classification=ArtifactClassification.PATCH,
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_operation_count_limit_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    0,
                    path="src/a.py",
                    sha256="a" * 64,
                ),
                make_record(
                    1,
                    path="src/b.py",
                    sha256="b" * 64,
                ),
            )
        )

        plan = make_plan(
            result,
            make_policy(max_operations=1),
        )

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_duplicate_destination_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    0,
                    path="src/file.py",
                    sha256="a" * 64,
                ),
                make_record(
                    1,
                    path="src/file.py",
                    sha256="b" * 64,
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_case_conflicting_destination_is_rejected(self) -> None:
        result = make_validation_result(
            (
                make_record(
                    0,
                    path="Src/File.py",
                    sha256="a" * 64,
                ),
                make_record(
                    1,
                    path="src/file.py",
                    sha256="b" * 64,
                ),
            )
        )

        plan = make_plan(result)

        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

    def test_mismatched_request_is_rejected_before_planning(self) -> None:
        result = make_validation_result()
        policy = make_policy()
        request = make_request(result, policy)
        other = make_validation_result(
            validation_id="output-validation:other"
        )

        with self.assertRaises(ArtifactApplicationPlanError):
            build_artifact_application_plan(
                request,
                other,
                policy,
            )

    def test_mismatched_policy_is_rejected_before_planning(self) -> None:
        result = make_validation_result()
        first = make_policy()
        request = make_request(result, first)
        second = ArtifactApplicationPolicy(
            policy_id="policy:other",
        )

        with self.assertRaises(ArtifactApplicationPlanError):
            build_artifact_application_plan(
                request,
                result,
                second,
            )


class ArtifactApplicationPlanIntegrityTests(unittest.TestCase):
    def test_plan_json_round_trip(self) -> None:
        original = make_plan()

        restored = ArtifactApplicationPlan.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_plan_json_is_canonical(self) -> None:
        plan = make_plan()
        data = json.loads(plan.to_json())

        self.assertEqual(
            plan.to_json(),
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ),
        )

    def test_tampered_operation_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        data["operations"][0]["destination_path"] = "src/other.py"

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_tampered_manifest_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        data["rollback_manifest"][0][
            "artifact_sha256"
        ] = "b" * 64

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_tampered_operations_hash_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        data["operations_hash"] = "f" * 64

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_tampered_manifest_hash_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        data["rollback_manifest_hash"] = "f" * 64

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_tampered_plan_hash_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        data["requested_by"] = "ELMAN_OTHER"

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_missing_plan_hash_is_rejected(self) -> None:
        plan = make_plan()
        data = plan.to_dict()
        del data["plan_hash"]

        with self.assertRaises(
            ArtifactApplicationPlanIntegrityError
        ):
            ArtifactApplicationPlan.from_dict(data)

    def test_manifest_entry_hash_verifies(self) -> None:
        entry = make_plan().rollback_manifest[0]
        entry.verify_hash()

    def test_manifest_entry_round_trip(self) -> None:
        original = make_plan().rollback_manifest[0]

        restored = ArtifactRollbackEntry.from_dict(
            original.to_dict()
        )

        self.assertEqual(restored, original)

    def test_plan_is_frozen(self) -> None:
        plan = make_plan()

        with self.assertRaises(FrozenInstanceError):
            plan.decision = (  # type: ignore[misc]
                ArtifactApplicationDecision.REJECTED
            )


if __name__ == "__main__":
    unittest.main()
