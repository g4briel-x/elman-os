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
from elman_os.agent_output_validation import (
    AgentOutputValidation,
    AgentOutputValidationError,
    AgentOutputValidationIntegrityError,
    AgentOutputValidationPolicy,
    AgentOutputValidationRequest,
    AgentOutputValidationResult,
    AgentOutputValidationStatus,
    ArtifactClassification,
    ArtifactOperation,
    ArtifactValidationDecision,
)
from elman_os.agent_response_ingestion import (
    AgentResponseIngestion,
    AgentResponseIngestionRequest,
    AgentResponseIngestionResult,
)
from elman_os.execution_checkpoint import ExecutionCheckpoint
from elman_os.execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
)
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
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
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
RESUME_ISSUED = "2026-08-04T00:10:00Z"
DISPATCHED = "2026-08-04T00:20:00Z"
RECEIVED = "2026-08-04T00:30:00Z"
VALIDATED = "2026-08-04T00:40:00Z"


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


def make_artifact(
    path: str = "src/generated.py",
    *,
    sha256: str = "a" * 64,
    size_bytes: int = 120,
    media_type: str = "text/x-python",
    kind: str = "source",
    operation: str = "create",
    executable: bool = False,
    metadata: object | None = None,
) -> dict[str, object]:
    artifact: dict[str, object] = {
        "path": path,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "kind": kind,
        "operation": operation,
        "executable": executable,
    }
    if metadata is not None:
        artifact["metadata"] = metadata
    return artifact


def make_ingestion_result(
    *,
    outputs: dict[str, object] | None = None,
    status: AgentResponseStatus = AgentResponseStatus.SUCCEEDED,
) -> AgentResponseIngestionResult:
    step = ExecutionStep(
        step_id="step.one",
        title="Generate one artifact",
        capability_id="build",
        objective="Generate one validated artifact declaration",
        required_permissions=("build",),
    )
    plan = ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Validate agent output declarations",
        created_by="ELMAN_NEXUS",
        steps=(step,),
        status=PlanStatus.PENDING,
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
        approval_reference="approval:001",
        created_at=T2,
        rationale="Human operator approved resume",
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
    dispatch_result = StepDispatch(
        dispatch_request,
        resume_result,
        make_registry(),
    ).prepare(
        resume_result.updated_plan,
        resume_result.to_journal(),
    )

    effective_outputs = (
        outputs
        if outputs is not None
        else {"artifacts": [make_artifact()]}
    )
    if status is AgentResponseStatus.SUCCEEDED:
        response = AgentResponse(
            request_id=dispatch_result.agent_request.request_id,
            agent_id=dispatch_result.agent_id,
            status=status,
            summary="Artifact declarations produced",
            outputs=effective_outputs,
            evidence=("unit-tests-pass",),
            confidence=0.95,
        )
    elif status is AgentResponseStatus.BLOCKED:
        response = AgentResponse(
            request_id=dispatch_result.agent_request.request_id,
            agent_id=dispatch_result.agent_id,
            status=status,
            summary="Output generation blocked",
            outputs=effective_outputs,
            warnings=("human input required",),
            confidence=0.5,
        )
    else:
        response = AgentResponse(
            request_id=dispatch_result.agent_request.request_id,
            agent_id=dispatch_result.agent_id,
            status=status,
            summary="Output generation failed",
            outputs=effective_outputs,
            errors=("generation failed",),
            confidence=0.1,
        )

    ingestion_request = (
        AgentResponseIngestionRequest.from_dispatch_result(
            dispatch_result,
            response,
            received_at=RECEIVED,
        )
    )
    return AgentResponseIngestion(
        ingestion_request,
        dispatch_result,
        response,
    ).ingest(
        dispatch_result.updated_plan,
        dispatch_result.to_journal(),
    )


def make_policy(**overrides: object) -> AgentOutputValidationPolicy:
    return AgentOutputValidationPolicy(
        policy_id="policy:output-validation-001",
        **overrides,
    )


def make_validation(
    ingestion: AgentResponseIngestionResult,
    policy: AgentOutputValidationPolicy | None = None,
) -> AgentOutputValidation:
    effective_policy = policy or make_policy()
    request = AgentOutputValidationRequest.from_ingestion_result(
        ingestion,
        effective_policy,
        requested_at=VALIDATED,
    )
    return AgentOutputValidation(
        request,
        ingestion,
        effective_policy,
    )


class OutputValidationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        self.assertEqual(
            make_policy().policy_hash,
            make_policy().policy_hash,
        )

    def test_policy_json_round_trip(self) -> None:
        original = make_policy(max_artifacts=8)

        restored = AgentOutputValidationPolicy.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        self.assertEqual(restored.policy_hash, original.policy_hash)

    def test_policy_rejects_zero_limit(self) -> None:
        with self.assertRaises(AgentOutputValidationError):
            make_policy(max_artifacts=0)

    def test_policy_rejects_total_smaller_than_single_limit(self) -> None:
        with self.assertRaises(AgentOutputValidationError):
            make_policy(
                max_artifact_bytes=100,
                max_total_bytes=50,
            )

    def test_policy_rejects_unknown_review_kind(self) -> None:
        with self.assertRaises(AgentOutputValidationError):
            make_policy(review_kinds=("unknown",))

    def test_policy_rejects_overlapping_extensions(self) -> None:
        with self.assertRaises(AgentOutputValidationError):
            make_policy(
                review_extensions=(".exe",),
                forbidden_extensions=(".exe",),
            )


class OutputValidationRequestTests(unittest.TestCase):
    def test_request_captures_ingestion_boundary(self) -> None:
        ingestion = make_ingestion_result()
        policy = make_policy()

        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            policy,
            requested_at=VALIDATED,
        )
        seal = ingestion.to_journal().seal()

        self.assertEqual(
            request.ingestion_result_hash,
            ingestion.result_hash,
        )
        self.assertEqual(
            request.plan_state_hash,
            __import__("hashlib").sha256(
                ingestion.updated_plan.to_json().encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(request.journal_event_count, seal.event_count)
        self.assertEqual(request.policy_hash, policy.policy_hash)

    def test_request_default_identifier_is_deterministic(self) -> None:
        ingestion = make_ingestion_result()
        policy = make_policy()

        first = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            policy,
            requested_at=VALIDATED,
        )
        second = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            policy,
            requested_at=VALIDATED,
        )

        self.assertEqual(first.validation_id, second.validation_id)
        self.assertEqual(first.request_hash, second.request_hash)

    def test_request_accepts_explicit_identifier(self) -> None:
        ingestion = make_ingestion_result()

        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            make_policy(),
            requested_at=VALIDATED,
            validation_id="output-validation:operator-001",
        )

        self.assertEqual(
            request.validation_id,
            "output-validation:operator-001",
        )

    def test_request_json_round_trip(self) -> None:
        ingestion = make_ingestion_result()
        original = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            make_policy(),
            requested_at=VALIDATED,
        )

        restored = AgentOutputValidationRequest.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        ingestion = make_ingestion_result()
        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            make_policy(),
            requested_at=VALIDATED,
        )
        data = request.to_dict()
        data["agent_id"] = "ELMAN_OTHER"

        with self.assertRaises(
            AgentOutputValidationIntegrityError
        ):
            AgentOutputValidationRequest.from_dict(data)

    def test_request_rejects_non_utc_datetime(self) -> None:
        ingestion = make_ingestion_result()

        with self.assertRaises(AgentOutputValidationError):
            AgentOutputValidationRequest.from_ingestion_result(
                ingestion,
                make_policy(),
                requested_at=datetime(
                    2026,
                    8,
                    4,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_accepts_utc_datetime(self) -> None:
        ingestion = make_ingestion_result()

        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            make_policy(),
            requested_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(
            request.requested_at,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_request_is_frozen(self) -> None:
        ingestion = make_ingestion_result()
        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            make_policy(),
            requested_at=VALIDATED,
        )

        with self.assertRaises(FrozenInstanceError):
            request.plan_id = "plan:other"  # type: ignore[misc]


class OutputValidationConstructionTests(unittest.TestCase):
    def test_validation_accepts_matching_sources(self) -> None:
        validation = make_validation(make_ingestion_result())

        self.assertEqual(
            validation.request.policy_id,
            validation.policy.policy_id,
        )

    def test_validation_rejects_other_policy(self) -> None:
        ingestion = make_ingestion_result()
        first = make_policy()
        request = AgentOutputValidationRequest.from_ingestion_result(
            ingestion,
            first,
            requested_at=VALIDATED,
        )
        second = AgentOutputValidationPolicy(
            policy_id="policy:other",
        )

        with self.assertRaises(AgentOutputValidationError):
            AgentOutputValidation(request, ingestion, second)

    def test_validation_rejects_other_ingestion_result(self) -> None:
        first = make_ingestion_result()
        second = make_ingestion_result(
            outputs={
                "artifacts": [
                    make_artifact(path="docs/report.md")
                ]
            }
        )
        request = AgentOutputValidationRequest.from_ingestion_result(
            first,
            make_policy(),
            requested_at=VALIDATED,
        )

        with self.assertRaises(AgentOutputValidationError):
            AgentOutputValidation(
                request,
                second,
                make_policy(),
            )


class AcceptedOutputTests(unittest.TestCase):
    def test_valid_artifact_is_accepted(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.ACCEPTED,
        )
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 0)

    def test_singular_artifact_is_supported(self) -> None:
        ingestion = make_ingestion_result(
            outputs={"artifact": make_artifact()}
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.ACCEPTED,
        )
        self.assertEqual(len(result.records), 1)

    def test_multiple_safe_artifacts_are_accepted(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifacts": [
                    make_artifact(
                        path="src/generated.py",
                        sha256="a" * 64,
                    ),
                    make_artifact(
                        path="tests/test_generated.py",
                        sha256="b" * 64,
                        kind="test",
                    ),
                    make_artifact(
                        path="docs/generated.md",
                        sha256="c" * 64,
                        media_type="text/markdown",
                        kind="documentation",
                    ),
                ]
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.ACCEPTED,
        )
        self.assertEqual(result.accepted_count, 3)
        self.assertEqual(result.total_declared_bytes, 360)

    def test_metadata_object_is_allowed(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(
                    metadata={"generator": "ELMAN_CORE"}
                )
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.records[0].decision,
            ArtifactValidationDecision.ACCEPTED,
        )

    def test_zero_byte_artifact_is_allowed(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(size_bytes=0)
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.ACCEPTED,
        )
        self.assertEqual(result.total_declared_bytes, 0)

    def test_result_does_not_mutate_ingestion(self) -> None:
        ingestion = make_ingestion_result()
        before = ingestion.to_json()

        make_validation(ingestion).validate()

        self.assertEqual(ingestion.to_json(), before)

    def test_validation_is_deterministic(self) -> None:
        validation = make_validation(make_ingestion_result())

        first = validation.validate()
        second = validation.validate()

        self.assertEqual(first.to_json(), second.to_json())


class ReviewOutputTests(unittest.TestCase):
    def test_no_artifact_requires_review(self) -> None:
        result = make_validation(
            make_ingestion_result(outputs={})
        ).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )

    def test_unknown_top_level_key_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifacts": [make_artifact()],
                "notes": "extra output",
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )
        self.assertTrue(result.top_level_reasons)

    def test_update_operation_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(operation="update")
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.records[0].decision,
            ArtifactValidationDecision.REQUIRES_REVIEW,
        )
        self.assertIs(
            result.records[0].operation,
            ArtifactOperation.UPDATE,
        )

    def test_executable_artifact_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(executable=True)
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )

    def test_patch_kind_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(
                    path="changes/update.diff",
                    media_type="text/x-diff",
                    kind="patch",
                )
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.records[0].classification,
            ArtifactClassification.PATCH,
        )
        self.assertIs(
            result.records[0].decision,
            ArtifactValidationDecision.REQUIRES_REVIEW,
        )

    def test_archive_extension_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(
                    path="dist/package.zip",
                    media_type="application/zip",
                    kind="archive",
                )
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )

    def test_sensitive_path_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifact": make_artifact(
                    path=".github/workflows/generated.yml",
                    media_type="application/yaml",
                    kind="configuration",
                )
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )

    def test_same_hash_for_different_paths_requires_review(self) -> None:
        ingestion = make_ingestion_result(
            outputs={
                "artifacts": [
                    make_artifact(
                        path="src/one.py",
                        sha256="a" * 64,
                    ),
                    make_artifact(
                        path="src/two.py",
                        sha256="a" * 64,
                    ),
                ]
            }
        )

        result = make_validation(ingestion).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REQUIRES_REVIEW,
        )
        self.assertEqual(result.review_count, 2)


class RejectedOutputTests(unittest.TestCase):
    def assert_rejected(
        self,
        outputs: dict[str, object],
        *,
        policy: AgentOutputValidationPolicy | None = None,
    ) -> AgentOutputValidationResult:
        result = make_validation(
            make_ingestion_result(outputs=outputs),
            policy,
        ).validate()
        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REJECTED,
        )
        return result

    def test_non_succeeded_response_is_rejected(self) -> None:
        result = make_validation(
            make_ingestion_result(
                outputs={},
                status=AgentResponseStatus.BLOCKED,
            )
        ).validate()

        self.assertIs(
            result.status,
            AgentOutputValidationStatus.REJECTED,
        )

    def test_both_artifact_forms_are_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(),
                "artifacts": [make_artifact()],
            }
        )

    def test_artifacts_object_instead_of_array_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifacts": make_artifact()}
        )

    def test_non_object_artifact_is_rejected(self) -> None:
        result = self.assert_rejected(
            {"artifacts": ["invalid"]}
        )

        self.assertEqual(result.rejected_count, 1)

    def test_missing_fields_are_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": {"path": "src/file.py"}}
        )

    def test_unknown_artifact_field_is_rejected(self) -> None:
        artifact = make_artifact()
        artifact["command"] = "run me"

        self.assert_rejected({"artifact": artifact})

    def test_parent_traversal_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path="../secret.txt")}
        )

    def test_absolute_path_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path="/tmp/file.txt")}
        )

    def test_windows_drive_path_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path="C:/temp/file.txt")}
        )

    def test_backslash_path_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path=r"src\file.py")}
        )

    def test_windows_reserved_name_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path="docs/CON.txt")}
        )

    def test_non_portable_character_is_rejected(self) -> None:
        self.assert_rejected(
            {"artifact": make_artifact(path="docs/a?.txt")}
        )

    def test_uppercase_hash_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    sha256="A" * 64
                )
            }
        )

    def test_invalid_media_type_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    media_type="Text/Python"
                )
            }
        )

    def test_invalid_kind_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    kind="unknown"
                )
            }
        )

    def test_invalid_operation_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    operation="delete"
                )
            }
        )

    def test_non_boolean_executable_is_rejected(self) -> None:
        artifact = make_artifact()
        artifact["executable"] = "yes"

        self.assert_rejected({"artifact": artifact})

    def test_non_object_metadata_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    metadata="not-an-object"
                )
            }
        )

    def test_forbidden_executable_extension_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    path="bin/tool.exe",
                    media_type="application/octet-stream",
                    kind="binary",
                )
            }
        )

    def test_sensitive_filename_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    path="config/.env",
                    kind="configuration",
                )
            }
        )

    def test_sensitive_suffix_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    path="certificates/private.pem",
                    media_type="application/x-pem-file",
                    kind="configuration",
                )
            }
        )

    def test_duplicate_path_is_rejected(self) -> None:
        result = self.assert_rejected(
            {
                "artifacts": [
                    make_artifact(),
                    make_artifact(),
                ]
            }
        )

        self.assertEqual(result.rejected_count, 2)

    def test_case_insensitive_path_conflict_is_rejected(self) -> None:
        result = self.assert_rejected(
            {
                "artifacts": [
                    make_artifact(
                        path="Src/File.py",
                        sha256="a" * 64,
                    ),
                    make_artifact(
                        path="src/file.py",
                        sha256="b" * 64,
                    ),
                ]
            }
        )

        self.assertEqual(result.rejected_count, 2)

    def test_artifact_size_limit_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifact": make_artifact(
                    size_bytes=101
                )
            },
            policy=make_policy(
                max_artifact_bytes=100,
                max_total_bytes=200,
            ),
        )

    def test_total_size_limit_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifacts": [
                    make_artifact(
                        path="src/a.py",
                        sha256="a" * 64,
                        size_bytes=60,
                    ),
                    make_artifact(
                        path="src/b.py",
                        sha256="b" * 64,
                        size_bytes=60,
                    ),
                ]
            },
            policy=make_policy(
                max_artifact_bytes=100,
                max_total_bytes=100,
            ),
        )

    def test_artifact_count_limit_is_rejected(self) -> None:
        self.assert_rejected(
            {
                "artifacts": [
                    make_artifact(
                        path="src/a.py",
                        sha256="a" * 64,
                    ),
                    make_artifact(
                        path="src/b.py",
                        sha256="b" * 64,
                    ),
                ]
            },
            policy=make_policy(max_artifacts=1),
        )


class OutputValidationResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        original = make_validation(
            make_ingestion_result()
        ).validate()

        restored = AgentOutputValidationResult.from_json(
            original.to_json()
        )

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_result_json_is_canonical(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()
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
        result = make_validation(
            make_ingestion_result()
        ).validate()
        data = result.to_dict()
        data["records"][0]["path"] = "src/tampered.py"

        with self.assertRaises(
            AgentOutputValidationIntegrityError
        ):
            AgentOutputValidationResult.from_dict(data)

    def test_tampered_count_is_rejected(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()
        data = result.to_dict()
        data["accepted_count"] = 0

        with self.assertRaises(
            AgentOutputValidationIntegrityError
        ):
            AgentOutputValidationResult.from_dict(data)

    def test_tampered_total_size_is_rejected(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()
        data = result.to_dict()
        data["total_declared_bytes"] = 999

        with self.assertRaises(
            AgentOutputValidationIntegrityError
        ):
            AgentOutputValidationResult.from_dict(data)

    def test_missing_result_hash_is_rejected(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()
        data = result.to_dict()
        del data["result_hash"]

        with self.assertRaises(
            AgentOutputValidationIntegrityError
        ):
            AgentOutputValidationResult.from_dict(data)

    def test_result_is_frozen(self) -> None:
        result = make_validation(
            make_ingestion_result()
        ).validate()

        with self.assertRaises(FrozenInstanceError):
            result.status = (  # type: ignore[misc]
                AgentOutputValidationStatus.REJECTED
            )


if __name__ == "__main__":
    unittest.main()
