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
from elman_os.artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndex,
    ArtifactOrchestrationStateIndexEntry,
    ArtifactOrchestrationStateIndexEntryStatus,
    ArtifactOrchestrationStateIndexError,
    ArtifactOrchestrationStateIndexIntegrityError,
    ArtifactOrchestrationStateIndexNotFoundError,
    ArtifactOrchestrationStateIndexPolicy,
    ArtifactOrchestrationStateIndexReadError,
    ArtifactOrchestrationStateIndexResult,
    ArtifactOrchestrationStateIndexSnapshot,
    ArtifactOrchestrationStateIndexStatus,
)
from elman_os.artifact_orchestration_state_persistence import (
    CHECKPOINT_FILE_NAME,
    JOURNAL_FILE_NAME,
    MANIFEST_FILE_NAME,
    PLAN_FILE_NAME,
    ArtifactOrchestrationPersistenceFile,
    ArtifactOrchestrationStateManifest,
)
from elman_os.artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationReadError,
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


INDEXED_AT = "2026-08-05T03:50:00Z"
PERSISTED_AT = "2026-08-05T03:45:00Z"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_execution_state(kind: str, suffix: str):
    if kind not in {"running", "blocked", "completed"}:
        raise ValueError(kind)

    step_status = {
        "running": StepStatus.RUNNING,
        "blocked": StepStatus.BLOCKED,
        "completed": StepStatus.COMPLETED,
    }[kind]
    plan_status = {
        "running": PlanStatus.RUNNING,
        "blocked": PlanStatus.BLOCKED,
        "completed": PlanStatus.COMPLETED,
    }[kind]

    plan = ExecutionPlan(
        plan_id=f"plan:{suffix}",
        project_id=f"project:{suffix}",
        objective=f"Index orchestration state {suffix}",
        created_by="ELMAN_NEXUS",
        steps=(
            ExecutionStep(
                step_id="step.one",
                title="Apply artifacts",
                capability_id="artifact.apply",
                objective="Apply verified artifacts",
                assigned_agent_id="ELMAN_CORE",
                status=step_status,
            ),
        ),
        status=plan_status,
        requires_human_approval=True,
        approval_reference=f"approval:{suffix}",
    )

    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        "2026-08-05T03:30:00Z",
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        "2026-08-05T03:31:00Z",
        payload={"approval_reference": f"approval:{suffix}"},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        "2026-08-05T03:32:00Z",
        payload={"project_id": plan.project_id},
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        "2026-08-05T03:33:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        "2026-08-05T03:34:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )

    if kind == "blocked":
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            "2026-08-05T03:35:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"reason": "manual intervention required"},
        )
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            "2026-08-05T03:35:00Z",
            payload={"affected_step_id": "step.one"},
        )
    elif kind == "completed":
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            "2026-08-05T03:35:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"artifact_transaction": "committed"},
        )
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            "2026-08-05T03:35:00Z",
            payload={"affected_step_id": "step.one"},
        )

    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id=f"artifact-checkpoint:{sha(suffix)}",
        created_at="2026-08-05T03:36:00Z",
    )
    return plan, journal, checkpoint


def make_manifest(
    plan: ExecutionPlan,
    journal: ExecutionJournal,
    checkpoint: ExecutionCheckpoint,
    *,
    persistence_id: str,
):
    plan_payload = plan.to_json().encode("utf-8")
    journal_payload = journal.to_jsonl().encode("utf-8")
    checkpoint_payload = checkpoint.to_json().encode("utf-8")
    seal = journal.seal()
    checkpoint_hash = checkpoint.checkpoint_hash
    assert checkpoint_hash is not None

    files = (
        ArtifactOrchestrationPersistenceFile(
            path=PLAN_FILE_NAME,
            media_type="application/json",
            size_bytes=len(plan_payload),
            sha256=hashlib.sha256(plan_payload).hexdigest(),
        ),
        ArtifactOrchestrationPersistenceFile(
            path=JOURNAL_FILE_NAME,
            media_type="application/x-ndjson",
            size_bytes=len(journal_payload),
            sha256=hashlib.sha256(journal_payload).hexdigest(),
        ),
        ArtifactOrchestrationPersistenceFile(
            path=CHECKPOINT_FILE_NAME,
            media_type="application/json",
            size_bytes=len(checkpoint_payload),
            sha256=hashlib.sha256(checkpoint_payload).hexdigest(),
        ),
    )

    manifest = ArtifactOrchestrationStateManifest(
        persistence_id=persistence_id,
        request_hash=sha(f"request:{persistence_id}"),
        policy_id="policy:persistence-001",
        policy_hash=sha("persistence-policy"),
        orchestration_id="artifact-orchestration:" + sha(persistence_id),
        orchestration_result_hash=sha(
            f"orchestration-result:{persistence_id}"
        ),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        step_id="step.one",
        agent_id="ELMAN_CORE",
        transaction_id="artifact-transaction:" + sha(persistence_id),
        result_plan_state_hash=hashlib.sha256(
            plan_payload
        ).hexdigest(),
        result_journal_hash=seal.journal_hash,
        result_checkpoint_hash=checkpoint_hash,
        files=files,
        persisted_at=PERSISTED_AT,
    )

    return manifest, {
        PLAN_FILE_NAME: plan_payload,
        JOURNAL_FILE_NAME: journal_payload,
        CHECKPOINT_FILE_NAME: checkpoint_payload,
    }


def write_state(
    root: Path,
    *,
    kind: str = "completed",
    suffix: str = "001",
    persistence_id: str | None = None,
):
    effective_id = (
        persistence_id
        or f"orchestration-persistence:{sha(suffix)}"
    )
    plan, journal, checkpoint = make_execution_state(kind, suffix)
    manifest, payloads = make_manifest(
        plan,
        journal,
        checkpoint,
        persistence_id=effective_id,
    )
    storage_key = hashlib.sha256(
        effective_id.encode("utf-8")
    ).hexdigest()
    directory = root / storage_key
    directory.mkdir(parents=True)
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
    (directory / MANIFEST_FILE_NAME).write_text(
        manifest.to_json(),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "root": root,
        "directory": directory,
        "storage_key": storage_key,
        "persistence_id": effective_id,
        "plan": plan,
        "journal": journal,
        "checkpoint": checkpoint,
        "manifest": manifest,
        "payloads": payloads,
    }


def rewrite_manifest(context, manifest):
    context["manifest"] = manifest
    (context["directory"] / MANIFEST_FILE_NAME).write_text(
        manifest.to_json(),
        encoding="utf-8",
        newline="\n",
    )


def replace_file_entry(manifest, name: str, payload: bytes):
    files = tuple(
        replace(
            item,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        if item.path == name
        else item
        for item in manifest.files
    )
    return replace(manifest, files=files, manifest_hash=None)


def make_policy(**changes):
    values = {
        "policy_id": "policy:state-index-001",
        "reject_symlink_components": True,
        "require_exact_entry_set": True,
        "require_canonical_payloads": True,
        "require_compatible_checkpoint": True,
        "max_candidates": 100,
        "max_file_bytes": 64 * 1024 * 1024,
    }
    values.update(changes)
    return ArtifactOrchestrationStateIndexPolicy(**values)


def make_index(root: Path, *, policy=None, **changes):
    values = {
        "policy": policy or make_policy(),
        "state_root": root,
        "requested_by": "ELMAN_NEXUS",
        "indexed_at": INDEXED_AT,
    }
    values.update(changes)
    return ArtifactOrchestrationStateIndex(**values)


def snapshot_tree(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            result[relative] = path.read_bytes()
        else:
            result[relative] = None
    return result


class StateIndexPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(max_candidates=25)
        self.assertEqual(
            ArtifactOrchestrationStateIndexPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_builds_matching_restoration_policy(self):
        policy = make_policy(
            require_exact_entry_set=False,
            require_canonical_payloads=False,
            require_compatible_checkpoint=False,
            max_file_bytes=4096,
        )
        restoration = policy.to_restoration_policy()
        self.assertFalse(restoration.require_exact_entry_set)
        self.assertFalse(restoration.require_canonical_payloads)
        self.assertFalse(restoration.require_compatible_checkpoint)
        self.assertEqual(restoration.max_file_bytes, 4096)

    def test_policy_rejects_non_boolean(self):
        with self.assertRaises(ArtifactOrchestrationStateIndexError):
            make_policy(require_exact_entry_set="yes")

    def test_policy_rejects_zero_candidate_limit(self):
        with self.assertRaises(ArtifactOrchestrationStateIndexError):
            make_policy(max_candidates=0)

    def test_policy_rejects_zero_file_limit(self):
        with self.assertRaises(ArtifactOrchestrationStateIndexError):
            make_policy(max_file_bytes=0)

    def test_policy_rejects_unsupported_version(self):
        with self.assertRaises(ArtifactOrchestrationStateIndexError):
            ArtifactOrchestrationStateIndexPolicy(
                policy_id="policy:state-index-001",
                version=2,
            )

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class StateIndexEntryTests(unittest.TestCase):
    def test_valid_entry_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            entry = make_index(context["root"]).build().snapshot.entries[0]
            restored = ArtifactOrchestrationStateIndexEntry.from_json(
                entry.to_json()
            )
            self.assertEqual(restored, entry)
            restored.verify_hash()

    def test_altered_entry_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad-entry").mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            restored = ArtifactOrchestrationStateIndexEntry.from_json(
                entry.to_json()
            )
            self.assertEqual(restored, entry)

    def test_valid_entry_requires_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                ArtifactOrchestrationStateIndexEntry(
                    storage_key="a" * 64,
                    status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                    state_directory=Path(directory).as_posix(),
                    reason_code="verified-state",
                    reason="VALID",
                )

    def test_valid_entry_requires_matching_storage_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexEntry(
                    storage_key="a" * 64,
                    status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                    state_directory=Path(directory).as_posix(),
                    reason_code="verified-state",
                    reason="VALID",
                    persistence_id="orchestration-persistence:" + "b" * 64,
                    manifest_hash=sha("manifest"),
                    orchestration_result_hash=sha("result"),
                    plan_id="plan:001",
                    project_id="project:001",
                    checkpoint_id="checkpoint:001",
                    assessment_status=ResumeAssessmentStatus.TERMINAL,
                    can_resume=False,
                    persisted_at=PERSISTED_AT,
                    state_hash=sha("state"),
                )

    def test_nonvalid_entry_rejects_restored_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                ArtifactOrchestrationStateIndexEntry(
                    storage_key="a" * 64,
                    status=(
                        ArtifactOrchestrationStateIndexEntryStatus.ALTERED
                    ),
                    state_directory=Path(directory).as_posix(),
                    reason_code="state-integrity-failed",
                    reason="ALTERED",
                    assessment_status=ResumeAssessmentStatus.TERMINAL,
                )

    def test_entry_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad-entry").mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            data = entry.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexEntry.from_dict(data)

    def test_entry_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad-entry").mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            data = entry.to_dict()
            del data["entry_hash"]
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexEntry.from_dict(data)

    def test_entry_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad-entry").mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            with self.assertRaises(FrozenInstanceError):
                entry.reason = "other"  # type: ignore[misc]


class StateIndexSnapshotTests(unittest.TestCase):
    def test_snapshot_counts_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            (root / "bad-entry").mkdir()
            snapshot = make_index(root).build().snapshot
            self.assertEqual(snapshot.total_count, 2)
            self.assertEqual(snapshot.valid_count, 1)
            self.assertEqual(snapshot.altered_count, 1)
            self.assertEqual(snapshot.unreadable_count, 0)

    def test_snapshot_requires_sorted_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_state(root, suffix="001")
            second = write_state(root, suffix="002")
            snapshot = make_index(root).build().snapshot
            reversed_entries = tuple(reversed(snapshot.entries))
            if reversed_entries == snapshot.entries:
                self.skipTest("fixture order did not differ")
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                replace(
                    snapshot,
                    entries=reversed_entries,
                    snapshot_hash=None,
                )

    def test_snapshot_rejects_duplicate_storage_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            snapshot = make_index(root).build().snapshot
            entry = snapshot.entries[0]
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                replace(
                    snapshot,
                    entries=(entry, entry),
                    valid_count=2,
                    snapshot_hash=None,
                )

    def test_snapshot_rejects_count_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            snapshot = make_index(root).build().snapshot
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                replace(
                    snapshot,
                    valid_count=0,
                    snapshot_hash=None,
                )

    def test_snapshot_sorts_control_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".staging").mkdir()
            (root / ".locks").mkdir()
            snapshot = make_index(root).build().snapshot
            self.assertEqual(
                snapshot.ignored_control_entries,
                (".locks", ".staging"),
            )

    def test_snapshot_rejects_unknown_control_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_index(root).build().snapshot
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                replace(
                    snapshot,
                    ignored_control_entries=(".unknown",),
                    snapshot_hash=None,
                )

    def test_snapshot_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            snapshot = make_index(root).build().snapshot
            restored = ArtifactOrchestrationStateIndexSnapshot.from_json(
                snapshot.to_json()
            )
            self.assertEqual(restored, snapshot)
            restored.verify_hash()

    def test_snapshot_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            snapshot = make_index(root).build().snapshot
            data = snapshot.to_dict()
            data["requested_by"] = "ELMAN_CORE"
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexSnapshot.from_dict(data)

    def test_snapshot_rejects_entry_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            (root / "bad-entry").mkdir()
            snapshot = make_index(root).build().snapshot
            escaped = replace(
                snapshot.entries[0],
                state_directory=Path(directory).as_posix(),
                entry_hash=None,
            )
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                replace(
                    snapshot,
                    entries=(escaped,),
                    snapshot_hash=None,
                )

    def test_snapshot_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_index(Path(directory)).build().snapshot
            with self.assertRaises(FrozenInstanceError):
                snapshot.valid_count = 1  # type: ignore[misc]


class StateIndexExecutionTests(unittest.TestCase):
    def test_empty_root_produces_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_index(Path(directory)).build()
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateIndexStatus.INDEXED,
            )
            self.assertEqual(result.snapshot.total_count, 0)

    def test_completed_state_is_valid_and_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="completed")
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.VALID,
            )
            self.assertIs(
                entry.assessment_status,
                ResumeAssessmentStatus.TERMINAL,
            )
            self.assertFalse(entry.can_resume)

    def test_running_state_is_valid_and_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="running")
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.assessment_status,
                ResumeAssessmentStatus.READY,
            )
            self.assertTrue(entry.can_resume)

    def test_blocked_state_is_valid_and_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="blocked")
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.assessment_status,
                ResumeAssessmentStatus.BLOCKED,
            )
            self.assertFalse(entry.can_resume)

    def test_multiple_entries_are_sorted_by_storage_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="003")
            write_state(root, suffix="001")
            write_state(root, suffix="002")
            entries = make_index(root).build().snapshot.entries
            keys = [entry.storage_key for entry in entries]
            self.assertEqual(keys, sorted(keys))

    def test_control_directories_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".locks").mkdir()
            (root / ".staging").mkdir()
            snapshot = make_index(root).build().snapshot
            self.assertEqual(snapshot.total_count, 0)
            self.assertEqual(
                snapshot.ignored_control_entries,
                (".locks", ".staging"),
            )

    def test_index_id_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_index(root)
            second = make_index(root)
            self.assertEqual(first.index_id, second.index_id)

    def test_index_id_changes_with_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_index(root)
            second = make_index(
                root,
                indexed_at="2026-08-05T03:51:00Z",
            )
            self.assertNotEqual(first.index_id, second.index_id)

    def test_custom_index_id_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            index = make_index(
                Path(directory),
                index_id="orchestration-state-index:custom",
            )
            self.assertEqual(
                index.index_id,
                "orchestration-state-index:custom",
            )

    def test_same_inputs_produce_same_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            first = make_index(root).build().snapshot
            second = make_index(root).build().snapshot
            self.assertEqual(first.snapshot_hash, second.snapshot_hash)

    def test_build_does_not_modify_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            before = snapshot_tree(root)
            make_index(root).build()
            after = snapshot_tree(root)
            self.assertEqual(after, before)

    def test_build_creates_no_new_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            before = {
                item.relative_to(root).as_posix()
                for item in root.rglob("*")
            }
            make_index(root).build()
            after = {
                item.relative_to(root).as_posix()
                for item in root.rglob("*")
            }
            self.assertEqual(after, before)

    def test_max_candidates_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").mkdir()
            (root / "two").mkdir()
            policy = make_policy(max_candidates=1)
            with self.assertRaises(ArtifactOrchestrationStateIndexReadError):
                make_index(root, policy=policy).build()

    def test_index_object_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            index = make_index(Path(directory))
            with self.assertRaises(FrozenInstanceError):
                index.requested_by = "ELMAN_CORE"  # type: ignore[misc]


class StateIndexClassificationTests(unittest.TestCase):
    def test_invalid_directory_name_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "not-a-storage-key").mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )
            self.assertEqual(entry.reason_code, "invalid-storage-key")

    def test_regular_file_at_root_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ("a" * 64)).write_text("file", encoding="utf-8")
            entry = make_index(root).build().snapshot.entries[0]
            self.assertEqual(
                entry.reason_code,
                "non-directory-state-entry",
            )

    def test_symlink_root_entry_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / ("a" * 64)
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            entry = make_index(root).build().snapshot.entries[0]
            self.assertEqual(entry.reason_code, "symlink-state-entry")

    def test_missing_manifest_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ("a" * 64)).mkdir()
            entry = make_index(root).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_tampered_manifest_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["directory"] / MANIFEST_FILE_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            data["project_id"] = "project:tampered"
            path.write_text(canonical_json(data), encoding="utf-8")
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_tampered_plan_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["directory"] / PLAN_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_invalid_utf8_manifest_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["directory"] / MANIFEST_FILE_NAME).write_bytes(
                b"\xff\xfe"
            )
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_noncanonical_manifest_is_altered_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["directory"] / MANIFEST_FILE_NAME
            path.write_text(
                context["manifest"].to_json() + "\n",
                encoding="utf-8",
            )
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_noncanonical_manifest_can_be_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["directory"] / MANIFEST_FILE_NAME
            path.write_text(
                context["manifest"].to_json() + "\n",
                encoding="utf-8",
            )
            policy = make_policy(require_canonical_payloads=False)
            entry = make_index(
                context["root"],
                policy=policy,
            ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.VALID,
            )

    def test_noncanonical_plan_is_altered_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["directory"] / PLAN_FILE_NAME
            payload = path.read_bytes() + b"\n"
            path.write_bytes(payload)
            manifest = replace_file_entry(
                context["manifest"],
                PLAN_FILE_NAME,
                payload,
            )
            manifest = replace(
                manifest,
                result_plan_state_hash=hashlib.sha256(payload).hexdigest(),
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_noncanonical_plan_can_be_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["directory"] / PLAN_FILE_NAME
            payload = path.read_bytes() + b"\n"
            path.write_bytes(payload)
            manifest = replace_file_entry(
                context["manifest"],
                PLAN_FILE_NAME,
                payload,
            )
            manifest = replace(
                manifest,
                result_plan_state_hash=hashlib.sha256(payload).hexdigest(),
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            policy = make_policy(require_canonical_payloads=False)
            entry = make_index(
                context["root"],
                policy=policy,
            ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.VALID,
            )

    def test_unexpected_state_file_is_altered_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["directory"] / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            entry = make_index(context["root"]).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_unexpected_state_file_can_be_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["directory"] / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            policy = make_policy(require_exact_entry_set=False)
            entry = make_index(
                context["root"],
                policy=policy,
            ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.VALID,
            )

    def test_storage_key_manifest_mismatch_is_altered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = write_state(root)
            wrong = root / ("f" * 64)
            context["directory"].rename(wrong)
            entry = make_index(root).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            )

    def test_oversized_manifest_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            policy = make_policy(max_file_bytes=16)
            entry = make_index(
                context["root"],
                policy=policy,
            ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE,
            )

    def test_manifest_read_error_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            with patch(
                "elman_os.artifact_orchestration_state_index."
                "_read_regular_file",
                side_effect=ArtifactOrchestrationStateIndexReadError(
                    "injected read failure"
                ),
            ):
                entry = make_index(
                    context["root"]
                ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE,
            )

    def test_restoration_read_error_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            with patch(
                "elman_os.artifact_orchestration_state_index."
                "ArtifactOrchestrationStateRestoration.restore",
                side_effect=ArtifactOrchestrationRestorationReadError(
                    "injected restoration read failure"
                ),
            ):
                entry = make_index(
                    context["root"]
                ).build().snapshot.entries[0]
            self.assertIs(
                entry.status,
                ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE,
            )

    def test_mixed_classifications_are_counted_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, suffix="001")
            (root / "bad-entry").mkdir()
            unreadable = write_state(root, suffix="002")
            policy = make_policy()
            original = (
                __import__(
                    "elman_os.artifact_orchestration_state_index",
                    fromlist=["_read_regular_file"],
                )._read_regular_file
            )

            def selective_read(path, *, max_file_bytes):
                if unreadable["storage_key"] in path.parts:
                    raise ArtifactOrchestrationStateIndexReadError(
                        "injected unreadable state"
                    )
                return original(path, max_file_bytes=max_file_bytes)

            with patch(
                "elman_os.artifact_orchestration_state_index."
                "_read_regular_file",
                side_effect=selective_read,
            ):
                snapshot = make_index(
                    root,
                    policy=policy,
                ).build().snapshot

            self.assertEqual(snapshot.valid_count, 1)
            self.assertEqual(snapshot.altered_count, 1)
            self.assertEqual(snapshot.unreadable_count, 1)


class StateIndexRootFailureTests(unittest.TestCase):
    def test_missing_root_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(
                ArtifactOrchestrationStateIndexNotFoundError
            ):
                make_index(missing).build()

    def test_root_file_raises_integrity_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            path.write_text("file", encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                make_index(path).build()

    def test_symlink_root_raises_integrity_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            link = base / "state"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                make_index(link).build()

    def test_root_enumeration_error_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "elman_os.artifact_orchestration_state_index.os.scandir",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(
                    ArtifactOrchestrationStateIndexReadError
                ):
                    make_index(Path(directory)).build()

    def test_relative_root_is_rejected(self):
        with self.assertRaises(ArtifactOrchestrationStateIndexError):
            ArtifactOrchestrationStateIndex(
                policy=make_policy(),
                state_root="relative/state",
                requested_by="ELMAN_NEXUS",
                indexed_at=INDEXED_AT,
            )

    def test_non_utc_datetime_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactOrchestrationStateIndexError):
                make_index(
                    Path(directory),
                    indexed_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_utc_datetime_is_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            index = make_index(
                Path(directory),
                indexed_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            self.assertEqual(
                index.indexed_at,
                "2026-08-05T00:00:00.000000Z",
            )


class StateIndexResultTests(unittest.TestCase):
    def test_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            result = make_index(root).build()
            restored = ArtifactOrchestrationStateIndexResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)
            restored.verify_hash()

    def test_result_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_index(Path(directory)).build()
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexResult.from_dict(data)

    def test_result_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_index(Path(directory)).build()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexResult.from_dict(data)

    def test_result_rejects_snapshot_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_index(Path(directory)).build()
            data = result.to_dict()
            data["index_id"] = "orchestration-state-index:other"
            with self.assertRaises(
                ArtifactOrchestrationStateIndexIntegrityError
            ):
                ArtifactOrchestrationStateIndexResult.from_dict(data)

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_index(Path(directory)).build()
            with self.assertRaises(FrozenInstanceError):
                result.reason = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
