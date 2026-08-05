from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elman_os.agent_contracts import canonical_json
from elman_os.artifact_orchestration_state_persistence import (
    CHECKPOINT_FILE_NAME,
    JOURNAL_FILE_NAME,
    MANIFEST_FILE_NAME,
    PLAN_FILE_NAME,
    ArtifactOrchestrationPersistenceError,
    ArtifactOrchestrationPersistenceFile,
    ArtifactOrchestrationPersistenceIntegrityError,
    ArtifactOrchestrationPersistenceLockError,
    ArtifactOrchestrationPersistencePolicy,
    ArtifactOrchestrationPersistenceRequest,
    ArtifactOrchestrationPersistenceResult,
    ArtifactOrchestrationPersistenceStatus,
    ArtifactOrchestrationStateManifest,
    ArtifactOrchestrationStatePersistence,
)
from elman_os.artifact_transaction_lifecycle import (
    ArtifactTransactionLifecycleRoute,
    ArtifactTransactionLifecycleState,
)
from elman_os.artifact_transaction_orchestration_adapter import (
    ArtifactTransactionOrchestrationDecision,
    ArtifactTransactionOrchestrationRecord,
    ArtifactTransactionOrchestrationResult,
    ArtifactTransactionOrchestrationStatus,
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
    StepStatus,
)


PERSISTED_AT = "2026-08-05T02:30:00Z"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_hash(payload) -> str:
    return hashlib.sha256(
        canonical_json(dict(payload)).encode("utf-8")
    ).hexdigest()


def make_orchestration_result() -> ArtifactTransactionOrchestrationResult:
    plan = ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Persist orchestration state",
        created_by="ELMAN_NEXUS",
        steps=(
            ExecutionStep(
                step_id="step.one",
                title="Apply artifacts",
                capability_id="artifact.apply",
                objective="Apply verified artifacts",
                assigned_agent_id="ELMAN_CORE",
                status=StepStatus.COMPLETED,
            ),
        ),
        status=PlanStatus.COMPLETED,
        requires_human_approval=True,
        approval_reference="approval:001",
    )

    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        "2026-08-05T02:20:00Z",
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        "2026-08-05T02:21:00Z",
        payload={"approval_reference": "approval:001"},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        "2026-08-05T02:22:00Z",
        payload={"project_id": plan.project_id},
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        "2026-08-05T02:23:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        "2026-08-05T02:24:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    step_event = journal.append(
        ExecutionEventType.STEP_COMPLETED,
        "2026-08-05T02:25:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
        payload={
            "artifact_lifecycle_result_hash": sha("lifecycle-result"),
            "artifact_orchestration_decision": "complete-step",
        },
    )
    plan_event = journal.append(
        ExecutionEventType.PLAN_COMPLETED,
        "2026-08-05T02:25:00Z",
        payload={"affected_step_id": "step.one"},
    )

    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id="artifact-checkpoint:" + sha("checkpoint"),
        created_at="2026-08-05T02:25:00Z",
    )
    seal = journal.seal()
    checkpoint_hash = checkpoint.checkpoint_hash
    assert checkpoint_hash is not None
    step_event_hash = step_event.event_hash
    plan_event_hash = plan_event.event_hash
    assert step_event_hash is not None
    assert plan_event_hash is not None

    records = (
        ArtifactTransactionOrchestrationRecord(
            index=0,
            event_sequence=step_event.sequence,
            event_type=step_event.event_type,
            step_id=step_event.step_id,
            agent_id=step_event.agent_id,
            event_hash=step_event_hash,
            payload_hash=payload_hash(step_event.payload),
            reason="APPENDED: step completion persisted in journal",
        ),
        ArtifactTransactionOrchestrationRecord(
            index=1,
            event_sequence=plan_event.sequence,
            event_type=plan_event.event_type,
            step_id=plan_event.step_id,
            agent_id=plan_event.agent_id,
            event_hash=plan_event_hash,
            payload_hash=payload_hash(plan_event.payload),
            reason="APPENDED: plan completion persisted in journal",
        ),
    )

    return ArtifactTransactionOrchestrationResult(
        orchestration_id="artifact-orchestration:" + sha("orchestration"),
        status=ArtifactTransactionOrchestrationStatus.COMPLETED,
        decision=ArtifactTransactionOrchestrationDecision.COMPLETE_STEP,
        request_hash=sha("orchestration-request"),
        policy_id="policy:orchestration-001",
        policy_hash=sha("orchestration-policy"),
        lifecycle_id="transaction-lifecycle:" + sha("lifecycle"),
        lifecycle_result_hash=sha("lifecycle-result"),
        lifecycle_final_state=(
            ArtifactTransactionLifecycleState.COMMITTED
        ),
        lifecycle_route=ArtifactTransactionLifecycleRoute.APPLY,
        transaction_id="artifact-transaction:" + sha("transaction"),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        step_id="step.one",
        agent_id="ELMAN_CORE",
        source_plan_state_hash=sha("source-plan"),
        result_plan_state_hash=hashlib.sha256(
            plan.to_json().encode("utf-8")
        ).hexdigest(),
        source_plan_status=PlanStatus.RUNNING,
        result_plan_status=PlanStatus.COMPLETED,
        source_step_status=StepStatus.RUNNING,
        result_step_status=StepStatus.COMPLETED,
        source_journal_event_count=5,
        result_journal_event_count=seal.event_count,
        source_journal_head_hash=sha("source-journal-head"),
        result_journal_head_hash=seal.head_hash,
        source_journal_hash=sha("source-journal"),
        result_journal_hash=seal.journal_hash,
        source_checkpoint_id="checkpoint:source-001",
        source_checkpoint_hash=sha("source-checkpoint"),
        result_checkpoint_id=checkpoint.checkpoint_id,
        result_checkpoint_hash=checkpoint_hash,
        records=records,
        updated_plan_json=plan.to_json(),
        updated_journal_jsonl=journal.to_jsonl(),
        updated_checkpoint_json=checkpoint.to_json(),
        completed_at="2026-08-05T02:25:00Z",
        reason="COMPLETED: lifecycle integrated into orchestration",
    )


def make_context(root: Path, *, policy=None):
    result = make_orchestration_result()
    effective_policy = (
        policy
        or ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001",
        )
    )
    request = ArtifactOrchestrationPersistenceRequest.from_sources(
        result,
        effective_policy,
        state_root=root,
        requested_by="ELMAN_NEXUS",
        requested_at=PERSISTED_AT,
    )
    persistence = ArtifactOrchestrationStatePersistence(
        request,
        result,
        effective_policy,
    )
    return {
        "result": result,
        "policy": effective_policy,
        "request": request,
        "persistence": persistence,
    }


class PersistencePolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self) -> None:
        first = ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001"
        )
        second = ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001"
        )
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_json_round_trip(self) -> None:
        original = ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001",
            max_file_bytes=1024,
        )
        self.assertEqual(
            ArtifactOrchestrationPersistencePolicy.from_json(
                original.to_json()
            ),
            original,
        )

    def test_policy_rejects_non_boolean(self) -> None:
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001",
                fsync_files="yes",
            )

    def test_policy_rejects_zero_file_limit(self) -> None:
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001",
                max_file_bytes=0,
            )

    def test_policy_rejects_unsupported_version(self) -> None:
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001",
                version=2,
            )

    def test_policy_is_frozen(self) -> None:
        policy = ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001"
        )
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class PersistenceRequestTests(unittest.TestCase):
    def test_request_captures_result_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            request = context["request"]
            result = context["result"]
            self.assertEqual(
                request.orchestration_result_hash,
                result.result_hash,
            )
            self.assertEqual(
                request.result_journal_hash,
                result.result_journal_hash,
            )

    def test_request_identifier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_context(root)["request"]
            second = make_context(root)["request"]
            self.assertEqual(
                first.persistence_id,
                second.persistence_id,
            )

    def test_request_identifier_changes_with_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = make_context(base / "one")["request"]
            second = make_context(base / "two")["request"]
            self.assertNotEqual(
                first.persistence_id,
                second.persistence_id,
            )

    def test_request_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_context(Path(directory))["request"]
            restored = ArtifactOrchestrationPersistenceRequest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["request"]
            data = request.to_dict()
            data["agent_id"] = "ELMAN_OTHER"
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceRequest.from_dict(data)

    def test_request_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["request"]
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceRequest.from_dict(data)

    def test_request_rejects_relative_root(self) -> None:
        result = make_orchestration_result()
        policy = ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:persistence-001"
        )
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistenceRequest.from_sources(
                result,
                policy,
                state_root="relative/state",
                requested_by="ELMAN_NEXUS",
                requested_at=PERSISTED_AT,
            )

    def test_request_rejects_non_utc_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_orchestration_result()
            policy = ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001"
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceError
            ):
                ArtifactOrchestrationPersistenceRequest.from_sources(
                    result,
                    policy,
                    state_root=Path(directory),
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
            result = make_orchestration_result()
            policy = ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001"
            )
            request = ArtifactOrchestrationPersistenceRequest.from_sources(
                result,
                policy,
                state_root=Path(directory),
                requested_by="ELMAN_NEXUS",
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = make_context(Path(directory))["request"]
            with self.assertRaises(FrozenInstanceError):
                request.plan_id = "plan:other"  # type: ignore[misc]


class ManifestContractTests(unittest.TestCase):
    def test_file_entry_round_trip(self) -> None:
        entry = ArtifactOrchestrationPersistenceFile(
            path=PLAN_FILE_NAME,
            media_type="application/json",
            size_bytes=10,
            sha256=sha("payload"),
        )
        self.assertEqual(
            ArtifactOrchestrationPersistenceFile.from_dict(
                entry.to_dict()
            ),
            entry,
        )

    def test_file_entry_rejects_nested_path(self) -> None:
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistenceFile(
                path="nested/plan.json",
                media_type="application/json",
                size_bytes=10,
                sha256=sha("payload"),
            )

    def test_file_entry_rejects_negative_size(self) -> None:
        with self.assertRaises(
            ArtifactOrchestrationPersistenceError
        ):
            ArtifactOrchestrationPersistenceFile(
                path=PLAN_FILE_NAME,
                media_type="application/json",
                size_bytes=-1,
                sha256=sha("payload"),
            )

    def test_manifest_entries_are_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            manifest = persistence._manifest(
                persistence._payloads()
            )
            self.assertEqual(
                tuple(item.path for item in manifest.files),
                tuple(sorted(item.path for item in manifest.files)),
            )

    def test_manifest_requires_exact_three_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            manifest = persistence._manifest(
                persistence._payloads()
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceError
            ):
                replace(
                    manifest,
                    files=manifest.files[:2],
                    manifest_hash=None,
                )

    def test_manifest_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            original = persistence._manifest(
                persistence._payloads()
            )
            restored = ArtifactOrchestrationStateManifest.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_manifest_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            manifest = persistence._manifest(
                persistence._payloads()
            )
            data = manifest.to_dict()
            data["transaction_id"] = "artifact-transaction:" + "f" * 64
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationStateManifest.from_dict(data)

    def test_manifest_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            manifest = persistence._manifest(
                persistence._payloads()
            )
            data = manifest.to_dict()
            del data["manifest_hash"]
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationStateManifest.from_dict(data)

    def test_manifest_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            manifest = persistence._manifest(
                persistence._payloads()
            )
            with self.assertRaises(FrozenInstanceError):
                manifest.plan_id = "plan:other"  # type: ignore[misc]

    def test_file_entry_is_frozen(self) -> None:
        entry = ArtifactOrchestrationPersistenceFile(
            path=PLAN_FILE_NAME,
            media_type="application/json",
            size_bytes=10,
            sha256=sha("payload"),
        )
        with self.assertRaises(FrozenInstanceError):
            entry.path = "other.json"  # type: ignore[misc]


class PersistenceExecutionTests(unittest.TestCase):
    def test_persist_creates_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            result = persistence.persist()
            self.assertTrue(Path(result.state_directory).is_dir())

    def test_persist_returns_persisted_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            self.assertIs(
                result.status,
                ArtifactOrchestrationPersistenceStatus.PERSISTED,
            )

    def test_persist_writes_exact_four_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            result = persistence.persist()
            self.assertEqual(
                {item.name for item in Path(result.state_directory).iterdir()},
                {
                    PLAN_FILE_NAME,
                    JOURNAL_FILE_NAME,
                    CHECKPOINT_FILE_NAME,
                    MANIFEST_FILE_NAME,
                },
            )

    def test_persisted_payloads_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            result = context["persistence"].persist()
            state = Path(result.state_directory)
            orchestration = context["result"]
            self.assertEqual(
                (state / PLAN_FILE_NAME).read_text(encoding="utf-8"),
                orchestration.updated_plan_json,
            )
            self.assertEqual(
                (state / JOURNAL_FILE_NAME).read_text(encoding="utf-8"),
                orchestration.updated_journal_jsonl,
            )
            self.assertEqual(
                (state / CHECKPOINT_FILE_NAME).read_text(
                    encoding="utf-8"
                ),
                orchestration.updated_checkpoint_json,
            )

    def test_manifest_links_orchestration_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            result = context["persistence"].persist()
            self.assertEqual(
                result.manifest.orchestration_result_hash,
                context["result"].result_hash,
            )

    def test_state_directory_key_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            expected = hashlib.sha256(
                persistence.request.persistence_id.encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                persistence.state_directory.name,
                expected,
            )

    def test_lock_is_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.persist()
            self.assertFalse(persistence.lock_path.exists())

    def test_staging_directory_is_removed_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.persist()
            self.assertFalse(
                persistence.staging_directory.exists()
            )

    def test_no_temporary_files_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.persist()
            self.assertEqual(
                list(Path(directory).rglob("*.tmp")),
                [],
            )

    def test_persisted_state_reconstructs_execution_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            state = Path(result.state_directory)
            plan = ExecutionPlan.from_json(
                (state / PLAN_FILE_NAME).read_text(encoding="utf-8")
            )
            journal = ExecutionJournal.from_jsonl(
                (state / JOURNAL_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            checkpoint = ExecutionCheckpoint.from_json(
                (state / CHECKPOINT_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            assessment = checkpoint.assess_resume(plan, journal)
            self.assertEqual(assessment.status.value, "terminal")

    def test_directory_fsync_disabled_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001",
                require_directory_fsync=False,
            )
            result = make_context(
                Path(directory),
                policy=policy,
            )["persistence"].persist()
            self.assertTrue(Path(result.state_directory).exists())

    def test_file_size_policy_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:persistence-001",
                max_file_bytes=1,
            )
            persistence = make_context(
                Path(directory),
                policy=policy,
            )["persistence"]
            with self.assertRaises(
                ArtifactOrchestrationPersistenceError
            ):
                persistence.persist()

    def test_constructor_rejects_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            bad_request = replace(
                context["request"],
                plan_id="plan:other",
                request_hash=None,
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceError
            ):
                ArtifactOrchestrationStatePersistence(
                    bad_request,
                    context["result"],
                    context["policy"],
                )


class PersistenceIdempotenceAndSafetyTests(unittest.TestCase):
    def test_second_persist_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.persist()
            second = persistence.persist()
            self.assertIs(
                second.status,
                ArtifactOrchestrationPersistenceStatus.NOOP,
            )

    def test_noop_preserves_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            first = persistence.persist()
            second = persistence.persist()
            self.assertEqual(
                first.manifest_hash,
                second.manifest_hash,
            )

    def test_divergent_plan_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            result = persistence.persist()
            (Path(result.state_directory) / PLAN_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                persistence.persist()

    def test_divergent_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            result = persistence.persist()
            (Path(result.state_directory) / MANIFEST_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                persistence.persist()

    def test_unexpected_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            result = persistence.persist()
            (
                Path(result.state_directory) / "unexpected.txt"
            ).write_text("unexpected", encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                persistence.persist()

    def test_existing_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.lock_path.parent.mkdir(parents=True)
            persistence.lock_path.write_text(
                "occupied",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationPersistenceLockError
            ):
                persistence.persist()

    def test_symlink_boundary_check_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            with patch(
                "elman_os.artifact_orchestration_state_persistence."
                "_reject_symlink_components",
                side_effect=(
                    ArtifactOrchestrationPersistenceIntegrityError(
                        "symlink path component is forbidden"
                    )
                ),
            ):
                with self.assertRaises(
                    ArtifactOrchestrationPersistenceIntegrityError
                ):
                    persistence.persist()

    def test_write_failure_rolls_back_new_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            real_write = (
                __import__(
                    "elman_os.artifact_orchestration_state_persistence",
                    fromlist=["_write_atomic"],
                )._write_atomic
            )
            calls = {"count": 0}

            def fail_second(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected write failure")
                return real_write(*args, **kwargs)

            with patch(
                "elman_os.artifact_orchestration_state_persistence."
                "_write_atomic",
                side_effect=fail_second,
            ):
                with self.assertRaises(OSError):
                    persistence.persist()
            self.assertFalse(
                persistence.staging_directory.exists()
            )
            self.assertFalse(persistence.state_directory.exists())
            self.assertFalse(persistence.lock_path.exists())

    def test_rename_failure_rolls_back_new_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            with patch(
                "elman_os.artifact_orchestration_state_persistence."
                "os.rename",
                side_effect=OSError("injected rename failure"),
            ):
                with self.assertRaises(OSError):
                    persistence.persist()
            self.assertFalse(
                persistence.staging_directory.exists()
            )
            self.assertFalse(persistence.state_directory.exists())

    def test_complete_preexisting_staging_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            payloads = persistence._payloads()
            manifest = persistence._manifest(payloads)
            persistence.state_root.mkdir(parents=True, exist_ok=True)
            persistence.staging_directory.parent.mkdir(parents=True)
            persistence.staging_directory.mkdir()
            persistence._write_staging(payloads, manifest)
            result = persistence.persist()
            self.assertIs(
                result.status,
                ArtifactOrchestrationPersistenceStatus.PERSISTED,
            )
            self.assertTrue(persistence.state_directory.exists())

    def test_incomplete_preexisting_staging_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = make_context(
                Path(directory)
            )["persistence"]
            persistence.state_root.mkdir(parents=True, exist_ok=True)
            persistence.staging_directory.parent.mkdir(parents=True)
            persistence.staging_directory.mkdir()
            (
                persistence.staging_directory / PLAN_FILE_NAME
            ).write_text("{}", encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                persistence.persist()
            self.assertTrue(
                persistence.staging_directory.exists()
            )

    def test_files_outside_state_root_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            persistence = make_context(
                base / "state"
            )["persistence"]
            persistence.persist()
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "unchanged",
            )


class PersistenceResultTests(unittest.TestCase):
    def test_result_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_context(
                Path(directory)
            )["persistence"].persist()
            restored = ArtifactOrchestrationPersistenceResult.from_json(
                original.to_json()
            )
            self.assertEqual(restored, original)
            restored.verify_hash()

    def test_result_rejects_tampered_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceResult.from_dict(data)

    def test_result_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceResult.from_dict(data)

    def test_result_rejects_state_directory_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            data = result.to_dict()
            data["state_directory"] = Path(directory).parent.as_posix()
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceResult.from_dict(data)

    def test_result_rejects_tampered_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = make_context(
                Path(directory)
            )["persistence"].persist()
            data = result.to_dict()
            manifest = json.loads(data["manifest_json"])
            manifest["project_id"] = "project:other"
            data["manifest_json"] = canonical_json(manifest)
            with self.assertRaises(
                ArtifactOrchestrationPersistenceIntegrityError
            ):
                ArtifactOrchestrationPersistenceResult.from_dict(data)


if __name__ == "__main__":
    unittest.main()
