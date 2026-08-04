from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
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
    ArtifactApplicationPolicy,
    ArtifactApplicationRequest,
    build_artifact_application_plan,
)
from elman_os.artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerification,
    ArtifactPayloadVerificationDecision,
    ArtifactPayloadVerificationError,
    ArtifactPayloadVerificationIntegrityError,
    ArtifactPayloadVerificationPolicy,
    ArtifactPayloadVerificationRequest,
    ArtifactPayloadVerificationResult,
    ArtifactPayloadVerificationStatus,
)


VALIDATED = "2026-08-04T03:00:00Z"
PLANNED = "2026-08-04T03:10:00Z"
VERIFIED = "2026-08-04T03:20:00Z"


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
    *,
    validation_id: str = "output-validation:001",
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
        validation_id=validation_id,
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


def default_specs() -> tuple[
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
            "src/generated.txt",
            b"hello from elman\n",
            "text/plain",
            ArtifactClassification.SOURCE,
            ArtifactOperation.CREATE,
        ),
    )


def make_application_plan(
    specifications: tuple[
        tuple[
            str,
            bytes,
            str,
            ArtifactClassification,
            ArtifactOperation,
        ],
        ...,
    ] | None = None,
    *,
    approval_reference: str | None = None,
):
    specs = specifications or default_specs()
    validation = make_validation_result(specs)
    policy = ArtifactApplicationPolicy(
        policy_id="policy:artifact-application-001",
    )
    request = ArtifactApplicationRequest.from_validation_result(
        validation,
        policy,
        requested_by="ELMAN_NEXUS",
        requested_at=PLANNED,
        approval_reference=approval_reference,
    )
    plan = build_artifact_application_plan(
        request,
        validation,
        policy,
    )
    return plan, specs


def make_payloads(
    plan,
    specifications,
) -> tuple[ArtifactPayload, ...]:
    by_path = {
        path: (content, media_type)
        for path, content, media_type, _, _ in specifications
    }
    return tuple(
        ArtifactPayload(
            operation_id=operation.operation_id,
            destination_path=operation.destination_path,
            media_type=operation.media_type,
            content=by_path[operation.destination_path][0],
        )
        for operation in plan.operations
    )


def make_policy(
    **overrides: object,
) -> ArtifactPayloadVerificationPolicy:
    return ArtifactPayloadVerificationPolicy(
        policy_id="policy:payload-verification-001",
        **overrides,
    )


def make_verification(
    *,
    plan=None,
    specifications=None,
    payloads=None,
    policy=None,
):
    effective_plan, effective_specs = (
        (plan, specifications)
        if plan is not None
        else make_application_plan(specifications)
    )
    assert effective_plan is not None
    assert effective_specs is not None
    effective_payloads = (
        tuple(payloads)
        if payloads is not None
        else make_payloads(
            effective_plan,
            effective_specs,
        )
    )
    effective_policy = policy or make_policy()
    request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
        effective_plan,
        effective_policy,
        effective_payloads,
        requested_by="ELMAN_NEXUS",
        requested_at=VERIFIED,
    )
    return ArtifactPayloadVerification(
        request,
        effective_plan,
        effective_payloads,
        effective_policy,
    )


class ArtifactPayloadTests(unittest.TestCase):
    def test_payload_computes_size_and_sha256(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=b"abc",
        )

        self.assertEqual(payload.size_bytes, 3)
        self.assertEqual(payload.content_sha256, sha256(b"abc"))

    def test_payload_accepts_bytearray(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=bytearray(b"abc"),
        )

        self.assertIsInstance(payload.content, bytes)

    def test_payload_accepts_memoryview(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=memoryview(b"abc"),
        )

        self.assertEqual(payload.content, b"abc")

    def test_payload_rejects_non_bytes_content(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayload(
                operation_id="artifact-operation:001",
                destination_path="src/file.txt",
                media_type="text/plain",
                content="abc",
            )

    def test_payload_rejects_absolute_path(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayload(
                operation_id="artifact-operation:001",
                destination_path="/tmp/file.txt",
                media_type="text/plain",
                content=b"abc",
            )

    def test_payload_rejects_traversal_path(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayload(
                operation_id="artifact-operation:001",
                destination_path="../file.txt",
                media_type="text/plain",
                content=b"abc",
            )

    def test_payload_rejects_invalid_media_type(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayload(
                operation_id="artifact-operation:001",
                destination_path="src/file.txt",
                media_type="Text/Plain",
                content=b"abc",
            )

    def test_payload_json_round_trip(self) -> None:
        original = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=b"\x00abc\xff",
        )

        restored = ArtifactPayload.from_json(original.to_json())

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_payload_rejects_invalid_base64(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=b"abc",
        )
        data = payload.to_dict()
        data["content_base64"] = "***"

        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayload.from_dict(data)

    def test_payload_rejects_tampered_hash(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=b"abc",
        )
        data = payload.to_dict()
        data["content_base64"] = "YWJk"

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayload.from_dict(data)

    def test_payload_is_frozen(self) -> None:
        payload = ArtifactPayload(
            operation_id="artifact-operation:001",
            destination_path="src/file.txt",
            media_type="text/plain",
            content=b"abc",
        )

        with self.assertRaises(FrozenInstanceError):
            payload.content = b"other"  # type: ignore[misc]


class PayloadVerificationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        self.assertEqual(
            make_policy().policy_hash,
            make_policy().policy_hash,
        )

    def test_policy_json_round_trip(self) -> None:
        original = make_policy(max_payloads=8)

        restored = ArtifactPayloadVerificationPolicy.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        self.assertEqual(restored.policy_hash, original.policy_hash)

    def test_policy_rejects_zero_payload_limit(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            make_policy(max_payloads=0)

    def test_policy_rejects_total_below_single_limit(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            make_policy(
                max_payload_bytes=100,
                max_total_bytes=50,
            )

    def test_policy_rejects_non_boolean_utf8_flag(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            make_policy(validate_utf8_text="yes")

    def test_policy_rejects_overlapping_media_types(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            make_policy(
                review_media_types=("application/octet-stream",),
                forbidden_media_types=(
                    "application/octet-stream",
                ),
            )

    def test_policy_rejects_unknown_classification(self) -> None:
        with self.assertRaises(ArtifactPayloadVerificationError):
            make_policy(
                review_classifications=("unknown",)
            )


class PayloadVerificationRequestTests(unittest.TestCase):
    def test_request_captures_plan_and_manifest(self) -> None:
        plan, specs = make_application_plan()
        payloads = make_payloads(plan, specs)
        policy = make_policy()

        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            policy,
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )

        self.assertEqual(
            request.application_plan_hash,
            plan.plan_hash,
        )
        self.assertEqual(request.payload_count, 1)
        self.assertEqual(
            request.payload_total_bytes,
            len(b"hello from elman\n"),
        )
        self.assertEqual(request.policy_hash, policy.policy_hash)

    def test_request_identifier_is_deterministic(self) -> None:
        plan, specs = make_application_plan()
        payloads = make_payloads(plan, specs)
        policy = make_policy()

        first = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            policy,
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        second = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            policy,
            reversed(payloads),
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )

        self.assertEqual(first.verification_id, second.verification_id)
        self.assertEqual(first.request_hash, second.request_hash)

    def test_request_accepts_explicit_identifier(self) -> None:
        plan, specs = make_application_plan()
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            make_payloads(plan, specs),
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
            verification_id="payload-verification:operator-001",
        )

        self.assertEqual(
            request.verification_id,
            "payload-verification:operator-001",
        )

    def test_request_json_round_trip(self) -> None:
        plan, specs = make_application_plan()
        original = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            make_payloads(plan, specs),
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )

        restored = ArtifactPayloadVerificationRequest.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        plan, specs = make_application_plan()
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            make_payloads(plan, specs),
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        data = request.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationRequest.from_dict(data)

    def test_request_rejects_missing_hash(self) -> None:
        plan, specs = make_application_plan()
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            make_payloads(plan, specs),
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        data = request.to_dict()
        del data["request_hash"]

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        plan, specs = make_application_plan()

        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayloadVerificationRequest.from_plan_and_payloads(
                plan,
                make_policy(),
                make_payloads(plan, specs),
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_accepts_utc_datetime(self) -> None:
        plan, specs = make_application_plan()

        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            make_payloads(plan, specs),
            requested_by="ELMAN_NEXUS",
            requested_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(
            request.requested_at,
            "2026-08-04T00:00:00.000000Z",
        )


class PayloadVerificationConstructionTests(unittest.TestCase):
    def test_verification_accepts_matching_sources(self) -> None:
        verification = make_verification()

        self.assertEqual(
            verification.request.application_id,
            verification.application_plan.application_id,
        )

    def test_verification_rejects_other_policy(self) -> None:
        plan, specs = make_application_plan()
        payloads = make_payloads(plan, specs)
        first = make_policy()
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            first,
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        second = ArtifactPayloadVerificationPolicy(
            policy_id="policy:other",
        )

        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayloadVerification(
                request,
                plan,
                payloads,
                second,
            )

    def test_verification_rejects_other_plan(self) -> None:
        first, specs = make_application_plan()
        payloads = make_payloads(first, specs)
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            first,
            make_policy(),
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        second, _ = make_application_plan(
            (
                (
                    "docs/other.txt",
                    b"other",
                    "text/plain",
                    ArtifactClassification.DOCUMENTATION,
                    ArtifactOperation.CREATE,
                ),
            )
        )

        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayloadVerification(
                request,
                second,
                payloads,
                make_policy(),
            )

    def test_verification_rejects_changed_payload_set(self) -> None:
        plan, specs = make_application_plan()
        payloads = make_payloads(plan, specs)
        request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
            plan,
            make_policy(),
            payloads,
            requested_by="ELMAN_NEXUS",
            requested_at=VERIFIED,
        )
        changed = (
            ArtifactPayload(
                operation_id=payloads[0].operation_id,
                destination_path=payloads[0].destination_path,
                media_type=payloads[0].media_type,
                content=b"changed",
            ),
        )

        with self.assertRaises(ArtifactPayloadVerificationError):
            ArtifactPayloadVerification(
                request,
                plan,
                changed,
                make_policy(),
            )


class VerifiedPayloadTests(unittest.TestCase):
    def test_matching_payload_is_verified(self) -> None:
        result = make_verification().verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.VERIFIED,
        )
        self.assertEqual(result.verified_count, 1)
        self.assertEqual(result.rejected_count, 0)

    def test_multiple_payloads_follow_operation_order(self) -> None:
        specs = (
            (
                "tests/z.txt",
                b"z",
                "text/plain",
                ArtifactClassification.TEST,
                ArtifactOperation.CREATE,
            ),
            (
                "docs/a.txt",
                b"a",
                "text/plain",
                ArtifactClassification.DOCUMENTATION,
                ArtifactOperation.CREATE,
            ),
            (
                "src/m.txt",
                b"m",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )
        plan, specs = make_application_plan(specs)
        payloads = tuple(reversed(make_payloads(plan, specs)))

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=payloads,
        ).verify()

        self.assertEqual(
            tuple(
                record.destination_path
                for record in result.records
            ),
            (
                "docs/a.txt",
                "src/m.txt",
                "tests/z.txt",
            ),
        )

    def test_exact_binary_bytes_are_verified(self) -> None:
        content = b"\x00\x01\xfe\xff"
        specs = (
            (
                "data/blob.bin",
                content,
                "application/x-custom",
                ArtifactClassification.DATA,
                ArtifactOperation.CREATE,
            ),
        )

        result = make_verification(
            specifications=specs
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.VERIFIED,
        )
        self.assertEqual(result.payloads[0].content, content)

    def test_empty_payload_is_verified_when_expected(self) -> None:
        specs = (
            (
                "data/empty.txt",
                b"",
                "text/plain",
                ArtifactClassification.DATA,
                ArtifactOperation.CREATE,
            ),
        )

        result = make_verification(
            specifications=specs
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.VERIFIED,
        )

    def test_verification_preserves_plan_hash(self) -> None:
        plan, specs = make_application_plan()

        result = make_verification(
            plan=plan,
            specifications=specs,
        ).verify()

        self.assertEqual(
            result.application_plan_hash,
            plan.plan_hash,
        )

    def test_verification_does_not_mutate_payloads(self) -> None:
        plan, specs = make_application_plan()
        payloads = make_payloads(plan, specs)
        before = tuple(item.to_json() for item in payloads)

        make_verification(
            plan=plan,
            specifications=specs,
            payloads=payloads,
        ).verify()

        self.assertEqual(
            tuple(item.to_json() for item in payloads),
            before,
        )

    def test_verification_is_deterministic(self) -> None:
        verification = make_verification()

        first = verification.verify()
        second = verification.verify()

        self.assertEqual(first.to_json(), second.to_json())

    def test_result_contains_no_workspace_action(self) -> None:
        payload = make_verification().verify().to_json()

        self.assertNotIn("write_file", payload)
        self.assertNotIn("execute", payload)
        self.assertNotIn("backup_content", payload)


class ReviewPayloadTests(unittest.TestCase):
    def test_review_media_type_requires_review(self) -> None:
        content = b"\x00\x01"
        specs = (
            (
                "data/blob.bin",
                content,
                "application/octet-stream",
                ArtifactClassification.DATA,
                ArtifactOperation.CREATE,
            ),
        )

        result = make_verification(
            specifications=specs,
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REQUIRES_REVIEW,
        )
        self.assertEqual(result.review_count, 1)

    def test_review_classification_requires_review(self) -> None:
        specs = (
            (
                "config/settings.json",
                b"{}",
                "application/json",
                ArtifactClassification.CONFIGURATION,
                ArtifactOperation.CREATE,
            ),
        )
        policy = make_policy(
            review_classifications=("configuration",)
        )

        result = make_verification(
            specifications=specs,
            policy=policy,
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REQUIRES_REVIEW,
        )


class RejectedPayloadTests(unittest.TestCase):
    def test_non_ready_plan_is_rejected(self) -> None:
        specs = (
            (
                "src/generated.txt",
                b"updated",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.UPDATE,
            ),
        )
        plan, specs = make_application_plan(specs)
        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REQUIRES_APPROVAL,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_missing_payload_is_rejected(self) -> None:
        plan, specs = make_application_plan()

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )
        self.assertIn(
            "missing",
            result.records[0].reasons[0],
        )

    def test_unknown_operation_id_is_rejected(self) -> None:
        plan, specs = make_application_plan()
        payload = make_payloads(plan, specs)[0]
        extra = ArtifactPayload(
            operation_id="artifact-operation:unknown",
            destination_path=payload.destination_path,
            media_type=payload.media_type,
            content=payload.content,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload, extra),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_duplicate_operation_id_is_rejected(self) -> None:
        plan, specs = make_application_plan()
        payload = make_payloads(plan, specs)[0]

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload, payload),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )
        self.assertGreaterEqual(result.rejected_count, 2)

    def test_destination_mismatch_is_rejected(self) -> None:
        plan, specs = make_application_plan()
        original = make_payloads(plan, specs)[0]
        payload = ArtifactPayload(
            operation_id=original.operation_id,
            destination_path="src/other.txt",
            media_type=original.media_type,
            content=original.content,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload,),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_media_type_mismatch_is_rejected(self) -> None:
        plan, specs = make_application_plan()
        original = make_payloads(plan, specs)[0]
        payload = ArtifactPayload(
            operation_id=original.operation_id,
            destination_path=original.destination_path,
            media_type="application/json",
            content=original.content,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload,),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_size_mismatch_is_rejected(self) -> None:
        plan, specs = make_application_plan()
        original = make_payloads(plan, specs)[0]
        payload = ArtifactPayload(
            operation_id=original.operation_id,
            destination_path=original.destination_path,
            media_type=original.media_type,
            content=b"short",
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload,),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_sha256_mismatch_is_rejected(self) -> None:
        original_content = b"same-length-one"
        changed_content = b"same-length-two"
        self.assertEqual(
            len(original_content),
            len(changed_content),
        )
        specs = (
            (
                "src/file.txt",
                original_content,
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )
        plan, specs = make_application_plan(specs)
        original = make_payloads(plan, specs)[0]
        payload = ArtifactPayload(
            operation_id=original.operation_id,
            destination_path=original.destination_path,
            media_type=original.media_type,
            content=changed_content,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(payload,),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_payload_size_policy_limit_is_rejected(self) -> None:
        result = make_verification(
            policy=make_policy(
                max_payload_bytes=5,
                max_total_bytes=100,
            )
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_total_size_policy_limit_is_rejected(self) -> None:
        specs = (
            (
                "src/a.txt",
                b"123456",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
            (
                "src/b.txt",
                b"abcdef",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )
        result = make_verification(
            specifications=specs,
            policy=make_policy(
                max_payload_bytes=10,
                max_total_bytes=10,
            ),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_payload_count_limit_is_rejected(self) -> None:
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
        result = make_verification(
            specifications=specs,
            policy=make_policy(max_payloads=1),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_forbidden_media_type_is_rejected(self) -> None:
        content = b"MZ"
        specs = (
            (
                "data/blob.bin",
                content,
                "application/x-msdownload",
                ArtifactClassification.DATA,
                ArtifactOperation.CREATE,
            ),
        )
        result = make_verification(
            specifications=specs,
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_invalid_utf8_text_is_rejected(self) -> None:
        specs = (
            (
                "src/file.txt",
                b"\xff\xfe",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )

        result = make_verification(
            specifications=specs,
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )

    def test_utf8_validation_can_be_disabled(self) -> None:
        specs = (
            (
                "src/file.txt",
                b"\xff\xfe",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )

        result = make_verification(
            specifications=specs,
            policy=make_policy(validate_utf8_text=False),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.VERIFIED,
        )

    def test_case_conflicting_destinations_are_rejected(self) -> None:
        specs = (
            (
                "Src/File.txt",
                b"one",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
            (
                "src/file.txt",
                b"two",
                "text/plain",
                ArtifactClassification.SOURCE,
                ArtifactOperation.CREATE,
            ),
        )
        plan, specs = make_application_plan(specs)
        self.assertIs(
            plan.decision,
            ArtifactApplicationDecision.REJECTED,
        )

        result = make_verification(
            plan=plan,
            specifications=specs,
            payloads=(),
        ).verify()

        self.assertIs(
            result.status,
            ArtifactPayloadVerificationStatus.REJECTED,
        )


class PayloadVerificationResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        original = make_verification().verify()

        restored = ArtifactPayloadVerificationResult.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        result = make_verification().verify()
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

    def test_tampered_record_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        data["records"][0]["actual_size_bytes"] = 999

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_tampered_payload_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        data["payloads"][0]["content_base64"] = "Y2hhbmdlZA=="

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_tampered_count_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        data["verified_count"] = 0

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_tampered_manifest_hash_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        data["payload_manifest_hash"] = "f" * 64

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_tampered_result_hash_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        result = make_verification().verify()
        data = result.to_dict()
        del data["result_hash"]

        with self.assertRaises(
            ArtifactPayloadVerificationIntegrityError
        ):
            ArtifactPayloadVerificationResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        result = make_verification().verify()

        with self.assertRaises(FrozenInstanceError):
            result.status = (  # type: ignore[misc]
                ArtifactPayloadVerificationStatus.REJECTED
            )


if __name__ == "__main__":
    unittest.main()
