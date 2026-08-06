from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest

from elman_os.artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndex,
    ArtifactOrchestrationStateIndexPolicy,
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
    ArtifactOrchestrationRestorationPolicy,
)
from elman_os.artifact_orchestration_state_selection import (
    ArtifactOrchestrationStateSelectionPolicy,
    ArtifactOrchestrationStateSelectionRecord,
    ArtifactOrchestrationStateSelectionRequest,
    ArtifactOrchestrationStateSelectionResult,
    ArtifactOrchestrationStateSelectionStatus,
    ArtifactOrchestrationStateSelector,
)
from elman_os.artifact_orchestration_selected_state_restoration import (
    ArtifactOrchestrationSelectedStateRestoration,
    ArtifactOrchestrationSelectedStateRestorationError,
    ArtifactOrchestrationSelectedStateRestorationExecutionError,
    ArtifactOrchestrationSelectedStateRestorationIntegrityError,
    ArtifactOrchestrationSelectedStateRestorationPolicy,
    ArtifactOrchestrationSelectedStateRestorationRequest,
    ArtifactOrchestrationSelectedStateRestorationResult,
    ArtifactOrchestrationSelectedStateRestorationSelectionError,
    ArtifactOrchestrationSelectedStateRestorationStatus,
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


INDEXED_AT = "2026-08-06T00:00:00Z"
SELECTED_AT = "2026-08-06T00:05:00Z"
RESTORED_AT = "2026-08-06T00:10:00Z"
PERSISTED_AT = "2026-08-05T23:55:00Z"


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
        project_id="project:selected-restoration",
        objective="Restore one selected orchestration state",
        created_by="ELMAN_NEXUS",
        steps=(
            ExecutionStep(
                step_id="step.one",
                title="Apply verified artifacts",
                capability_id="artifact.apply",
                objective="Apply verified artifacts transactionally",
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
        "2026-08-05T23:40:00Z",
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        "2026-08-05T23:41:00Z",
        payload={"approval_reference": f"approval:{suffix}"},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        "2026-08-05T23:42:00Z",
        payload={"project_id": plan.project_id},
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        "2026-08-05T23:43:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        "2026-08-05T23:44:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )

    if kind == "blocked":
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            "2026-08-05T23:45:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"reason": "manual intervention required"},
        )
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            "2026-08-05T23:45:00Z",
            payload={"affected_step_id": "step.one"},
        )
    elif kind == "completed":
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            "2026-08-05T23:45:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"artifact_transaction": "committed"},
        )
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            "2026-08-05T23:45:00Z",
            payload={"affected_step_id": "step.one"},
        )

    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id=f"artifact-checkpoint:{sha(suffix)}",
        created_at="2026-08-05T23:46:00Z",
    )
    return plan, journal, checkpoint


def write_state(
    root: Path,
    *,
    kind: str = "running",
    suffix: str = "one",
    persisted_at: str = PERSISTED_AT,
):
    persistence_id = f"orchestration-persistence:{sha(suffix)}"
    plan, journal, checkpoint = make_execution_state(kind, suffix)
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
        request_hash=sha(f"persistence-request:{suffix}"),
        policy_id="policy:persistence-selected-restoration",
        policy_hash=sha("persistence-policy-selected-restoration"),
        orchestration_id=f"artifact-orchestration:{sha(suffix)}",
        orchestration_result_hash=sha(f"orchestration-result:{suffix}"),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        step_id="step.one",
        agent_id="ELMAN_CORE",
        transaction_id=f"artifact-transaction:{sha(suffix)}",
        result_plan_state_hash=hashlib.sha256(plan_payload).hexdigest(),
        result_journal_hash=seal.journal_hash,
        result_checkpoint_hash=checkpoint_hash,
        files=files,
        persisted_at=persisted_at,
    )

    storage_key = sha(persistence_id)
    state_directory = root / storage_key
    state_directory.mkdir(parents=True)
    (state_directory / PLAN_FILE_NAME).write_bytes(plan_payload)
    (state_directory / JOURNAL_FILE_NAME).write_bytes(journal_payload)
    (state_directory / CHECKPOINT_FILE_NAME).write_bytes(checkpoint_payload)
    (state_directory / MANIFEST_FILE_NAME).write_text(
        manifest.to_json(),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "root": root,
        "state_directory": state_directory,
        "storage_key": storage_key,
        "persistence_id": persistence_id,
        "plan": plan,
        "journal": journal,
        "checkpoint": checkpoint,
        "manifest": manifest,
    }


def snapshot_tree(root: Path):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            snapshot[relative] = path.read_bytes()
        else:
            snapshot[relative] = None
    return snapshot


def make_index_policy():
    return ArtifactOrchestrationStateIndexPolicy(
        policy_id="policy:selected-restoration-index",
        reject_symlink_components=True,
        require_exact_entry_set=True,
        require_canonical_payloads=True,
        require_compatible_checkpoint=True,
        max_candidates=100,
        max_file_bytes=64 * 1024 * 1024,
    )


def make_selection(root: Path, **request_changes):
    index = ArtifactOrchestrationStateIndex(
        policy=make_index_policy(),
        state_root=root,
        requested_by="ELMAN_NEXUS",
        indexed_at=INDEXED_AT,
    ).build()
    selection_policy = ArtifactOrchestrationStateSelectionPolicy(
        policy_id="policy:selected-restoration-selection",
        reject_ambiguous=True,
        max_snapshot_entries=100,
        max_eligible_entries=100,
    )
    values = {
        "request_id": "state-selection-request:selected-restoration",
        "snapshot_json": index.snapshot.to_json(),
        "requested_by": "ELMAN_NEXUS",
        "requested_at": SELECTED_AT,
        "expected_snapshot_hash": index.snapshot.snapshot_hash,
    }
    values.update(request_changes)
    selection_request = ArtifactOrchestrationStateSelectionRequest(**values)
    return ArtifactOrchestrationStateSelector(selection_policy).select(
        selection_request
    )


def make_policy(**restoration_changes):
    restoration_values = {
        "policy_id": "policy:selected-restoration-delegated",
        "reject_symlink_components": True,
        "require_exact_entry_set": True,
        "require_canonical_payloads": True,
        "require_compatible_checkpoint": True,
        "max_file_bytes": 64 * 1024 * 1024,
    }
    restoration_values.update(restoration_changes)
    return ArtifactOrchestrationSelectedStateRestorationPolicy(
        policy_id="policy:selected-state-restoration-001",
        restoration_policy=ArtifactOrchestrationRestorationPolicy(
            **restoration_values
        ),
    )


def make_request(selection, root: Path, policy=None, **changes):
    values = {
        "selection_result": selection,
        "policy": policy or make_policy(),
        "state_root": root,
        "requested_by": "ELMAN_NEXUS",
        "requested_at": RESTORED_AT,
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateRestorationRequest.from_selection_result(
        **values
    )


def restore_selected(selection, root: Path, policy=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(
        selection,
        root,
        policy=effective_policy,
        **request_changes,
    )
    return ArtifactOrchestrationSelectedStateRestoration(
        request,
        effective_policy,
    ).restore()


def rebind_selected_entry(selection, new_entry):
    eligible = [record for record in selection.records if record.rank_position == 1]
    if len(eligible) != 1:
        raise AssertionError("fixture requires one selected record")
    old_record = eligible[0]
    new_record = replace(
        old_record,
        entry_hash=new_entry.entry_hash,
        record_hash=None,
    )
    records = tuple(
        new_record if record is old_record else record
        for record in selection.records
    )
    return replace(
        selection,
        records=records,
        selected_entry_json=new_entry.to_json(),
        selected_record_hash=new_record.record_hash,
        result_hash=None,
    )


class SelectedStateRestorationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(max_file_bytes=4096)
        self.assertEqual(
            ArtifactOrchestrationSelectedStateRestorationPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_rejects_wrong_restoration_policy_type(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateRestorationError
        ):
            ArtifactOrchestrationSelectedStateRestorationPolicy(
                policy_id="policy:selected-state-restoration-001",
                restoration_policy="invalid",  # type: ignore[arg-type]
            )

    def test_policy_rejects_unsupported_version(self):
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateRestorationError
        ):
            ArtifactOrchestrationSelectedStateRestorationPolicy(
                policy_id="policy:selected-state-restoration-001",
                restoration_policy=ArtifactOrchestrationRestorationPolicy(
                    policy_id="policy:selected-restoration-delegated"
                ),
                version=2,
            )

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class SelectedStateRestorationRequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            first = make_request(selection, root)
            second = make_request(selection, root)
            self.assertEqual(
                first.selected_restoration_id,
                second.selected_restoration_id,
            )

    def test_request_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            request = make_request(make_selection(root), root)
            restored = (
                ArtifactOrchestrationSelectedStateRestorationRequest.from_json(
                    request.to_json()
                )
            )
            self.assertEqual(restored, request)
            restored.verify_hash()

    def test_request_normalizes_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            request = make_request(
                make_selection(root),
                root,
                requested_at=datetime(2026, 8, 6, 0, 10, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-06T00:10:00.000000Z",
            )

    def test_request_rejects_non_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationError
            ):
                make_request(
                    make_selection(root),
                    root,
                    requested_at=datetime(
                        2026,
                        8,
                        6,
                        1,
                        10,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_rejects_relative_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationError
            ):
                make_request(make_selection(root), Path("relative"))

    def test_request_rejects_no_match_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(
                root,
                persistence_id="orchestration-persistence:" + "f" * 64,
            )
            self.assertEqual(
                selection.status,
                ArtifactOrchestrationStateSelectionStatus.NO_MATCH,
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationSelectionError
            ):
                make_request(selection, root)

    def test_request_rejects_wrong_selection_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            data = {
                "selected_restoration_id": "selected-state-restoration:test",
                "policy_id": make_policy().policy_id,
                "policy_hash": make_policy().policy_hash,
                "selection_result_json": selection.to_json(),
                "selection_result_hash": sha("wrong"),
                "state_root": root,
                "requested_by": "ELMAN_NEXUS",
                "requested_at": RESTORED_AT,
            }
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                ArtifactOrchestrationSelectedStateRestorationRequest(**data)

    def test_request_rejects_selected_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            entry = selection.selected_entry
            assert entry is not None
            changed = replace(
                entry,
                state_directory=(root / "wrong").as_posix(),
                entry_hash=None,
            )
            selection = rebind_selected_entry(selection, changed)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                make_request(selection, root)

    def test_request_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            data = make_request(make_selection(root), root).to_dict()
            data["requested_by"] = "ELMAN_CORE"
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                ArtifactOrchestrationSelectedStateRestorationRequest.from_dict(
                    data
                )

    def test_request_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            data = make_request(make_selection(root), root).to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                ArtifactOrchestrationSelectedStateRestorationRequest.from_dict(
                    data
                )

    def test_request_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            request = make_request(make_selection(root), root)
            with self.assertRaises(FrozenInstanceError):
                request.policy_id = "policy:other"  # type: ignore[misc]


class SelectedStateRestorationExecutionTests(unittest.TestCase):
    def test_running_state_is_restored_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = write_state(root, kind="running")
            result = restore_selected(make_selection(root), root)
            self.assertEqual(
                result.status,
                ArtifactOrchestrationSelectedStateRestorationStatus.RESTORED,
            )
            self.assertEqual(
                result.restored_state.assessment_status,
                ResumeAssessmentStatus.READY,
            )
            self.assertTrue(result.restored_state.can_resume)
            self.assertEqual(
                result.restored_state.persistence_id,
                context["persistence_id"],
            )

    def test_blocked_state_is_restored_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, kind="blocked")
            result = restore_selected(make_selection(root), root)
            self.assertEqual(
                result.restored_state.assessment_status,
                ResumeAssessmentStatus.BLOCKED,
            )
            self.assertFalse(result.restored_state.can_resume)

    def test_completed_state_is_restored_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root, kind="completed")
            result = restore_selected(make_selection(root), root)
            self.assertEqual(
                result.restored_state.assessment_status,
                ResumeAssessmentStatus.TERMINAL,
            )
            self.assertFalse(result.restored_state.can_resume)

    def test_restoration_does_not_modify_persistence_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            before = snapshot_tree(root)
            restore_selected(selection, root)
            self.assertEqual(snapshot_tree(root), before)

    def test_same_inputs_produce_same_result_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            first = restore_selected(selection, root)
            second = restore_selected(selection, root)
            self.assertEqual(first.result_hash, second.result_hash)
            self.assertEqual(first.to_json(), second.to_json())

    def test_result_binds_selection_and_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            result = restore_selected(selection, root)
            self.assertEqual(
                result.selection_result.result_hash,
                selection.result_hash,
            )
            self.assertEqual(
                result.restoration_request_hash,
                result.restoration_result.request_hash,
            )
            self.assertEqual(
                result.selected_entry.persistence_id,
                result.restoration_result.persistence_id,
            )

    def test_manifest_change_after_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = write_state(root)
            selection = make_selection(root)
            (context["state_directory"] / MANIFEST_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationExecutionError
            ):
                restore_selected(selection, root)

    def test_missing_state_after_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = write_state(root)
            selection = make_selection(root)
            for path in context["state_directory"].iterdir():
                path.unlink()
            context["state_directory"].rmdir()
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationExecutionError
            ):
                restore_selected(selection, root)

    def test_selected_manifest_binding_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            entry = selection.selected_entry
            assert entry is not None
            changed = replace(
                entry,
                manifest_hash=sha("wrong-manifest"),
                entry_hash=None,
            )
            selection = rebind_selected_entry(selection, changed)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationExecutionError
            ):
                restore_selected(selection, root)

    def test_selected_plan_binding_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            entry = selection.selected_entry
            assert entry is not None
            changed = replace(
                entry,
                plan_id="plan:unexpected",
                entry_hash=None,
            )
            selection = rebind_selected_entry(selection, changed)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                restore_selected(selection, root)

    def test_executor_rejects_policy_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            selection = make_selection(root)
            first_policy = make_policy()
            request = make_request(
                selection,
                root,
                policy=first_policy,
            )
            second_policy = ArtifactOrchestrationSelectedStateRestorationPolicy(
                policy_id="policy:selected-state-restoration-002",
                restoration_policy=first_policy.restoration_policy,
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationError
            ):
                ArtifactOrchestrationSelectedStateRestoration(
                    request,
                    second_policy,
                )

    def test_executor_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            policy = make_policy()
            request = make_request(
                make_selection(root),
                root,
                policy=policy,
            )
            executor = ArtifactOrchestrationSelectedStateRestoration(
                request,
                policy,
            )
            with self.assertRaises(FrozenInstanceError):
                executor.policy = make_policy()  # type: ignore[misc]


class SelectedStateRestorationResultTests(unittest.TestCase):
    def test_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            result = restore_selected(make_selection(root), root)
            restored = (
                ArtifactOrchestrationSelectedStateRestorationResult.from_json(
                    result.to_json()
                )
            )
            self.assertEqual(restored, result)
            restored.verify_hash()

    def test_result_rejects_wrong_restoration_request_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            result = restore_selected(make_selection(root), root)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                replace(
                    result,
                    restoration_request_hash=sha("wrong"),
                    result_hash=None,
                )

    def test_result_rejects_wrong_completed_at(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            result = restore_selected(make_selection(root), root)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                replace(
                    result,
                    completed_at="2026-08-06T00:11:00Z",
                    result_hash=None,
                )

    def test_result_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            data = restore_selected(make_selection(root), root).to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                ArtifactOrchestrationSelectedStateRestorationResult.from_dict(
                    data
                )

    def test_result_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            data = restore_selected(make_selection(root), root).to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateRestorationIntegrityError
            ):
                ArtifactOrchestrationSelectedStateRestorationResult.from_dict(
                    data
                )

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_state(root)
            result = restore_selected(make_selection(root), root)
            with self.assertRaises(FrozenInstanceError):
                result.reason = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
