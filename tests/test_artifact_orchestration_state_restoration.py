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
    ArtifactOrchestrationPersistenceFile,
    ArtifactOrchestrationPersistenceResult,
    ArtifactOrchestrationPersistenceStatus,
    ArtifactOrchestrationStateManifest,
)
from elman_os.artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationError,
    ArtifactOrchestrationRestorationIntegrityError,
    ArtifactOrchestrationRestorationNotFoundError,
    ArtifactOrchestrationRestorationPolicy,
    ArtifactOrchestrationRestorationReadError,
    ArtifactOrchestrationRestorationRequest,
    ArtifactOrchestrationRestorationResult,
    ArtifactOrchestrationRestorationStatus,
    ArtifactOrchestrationRestoredState,
    ArtifactOrchestrationStateRestoration,
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


RESTORED_AT = "2026-08-05T03:05:00Z"
PERSISTED_AT = "2026-08-05T02:55:00Z"
PERSISTENCE_ID = "orchestration-persistence:" + "a" * 64


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_execution_state(kind: str = "completed"):
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
        plan_id="plan:001",
        project_id="project:001",
        objective="Restore verified orchestration state",
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
        approval_reference="approval:001",
    )

    journal = ExecutionJournal(plan.plan_id)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        "2026-08-05T02:40:00Z",
        payload={"objective": plan.objective},
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        "2026-08-05T02:41:00Z",
        payload={"approval_reference": "approval:001"},
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        "2026-08-05T02:42:00Z",
        payload={"project_id": plan.project_id},
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        "2026-08-05T02:43:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        "2026-08-05T02:44:00Z",
        step_id="step.one",
        agent_id="ELMAN_CORE",
    )

    if kind == "blocked":
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            "2026-08-05T02:45:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"reason": "manual intervention required"},
        )
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            "2026-08-05T02:45:00Z",
            payload={"affected_step_id": "step.one"},
        )
    elif kind == "completed":
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            "2026-08-05T02:45:00Z",
            step_id="step.one",
            agent_id="ELMAN_CORE",
            payload={"artifact_transaction": "committed"},
        )
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            "2026-08-05T02:45:00Z",
            payload={"affected_step_id": "step.one"},
        )

    checkpoint = ExecutionCheckpoint.capture(
        plan,
        journal,
        checkpoint_id="artifact-checkpoint:" + sha(kind),
        created_at="2026-08-05T02:46:00Z",
    )
    return plan, journal, checkpoint


def make_manifest(
    plan: ExecutionPlan,
    journal: ExecutionJournal,
    checkpoint: ExecutionCheckpoint,
    *,
    persistence_id: str = PERSISTENCE_ID,
    orchestration_result_hash: str | None = None,
):
    plan_payload = plan.to_json().encode("utf-8")
    journal_payload = journal.to_jsonl().encode("utf-8")
    checkpoint_payload = checkpoint.to_json().encode("utf-8")
    seal = journal.seal()
    checkpoint_hash = checkpoint.checkpoint_hash
    assert checkpoint_hash is not None

    entries = (
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
        request_hash=sha("persistence-request"),
        policy_id="policy:persistence-001",
        policy_hash=sha("persistence-policy"),
        orchestration_id="artifact-orchestration:" + sha("orchestration"),
        orchestration_result_hash=(
            orchestration_result_hash or sha("orchestration-result")
        ),
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        step_id="step.one",
        agent_id="ELMAN_CORE",
        transaction_id="artifact-transaction:" + sha("transaction"),
        result_plan_state_hash=hashlib.sha256(plan_payload).hexdigest(),
        result_journal_hash=seal.journal_hash,
        result_checkpoint_hash=checkpoint_hash,
        files=entries,
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
    persistence_id: str = PERSISTENCE_ID,
):
    plan, journal, checkpoint = make_execution_state(kind)
    manifest, payloads = make_manifest(
        plan,
        journal,
        checkpoint,
        persistence_id=persistence_id,
    )
    state_key = hashlib.sha256(
        persistence_id.encode("utf-8")
    ).hexdigest()
    state_directory = root / state_key
    state_directory.mkdir(parents=True)
    for name, payload in payloads.items():
        (state_directory / name).write_bytes(payload)
    (state_directory / MANIFEST_FILE_NAME).write_text(
        manifest.to_json(),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "root": root,
        "state_directory": state_directory,
        "persistence_id": persistence_id,
        "plan": plan,
        "journal": journal,
        "checkpoint": checkpoint,
        "manifest": manifest,
        "payloads": payloads,
    }


def rewrite_manifest(context, manifest):
    context["manifest"] = manifest
    (context["state_directory"] / MANIFEST_FILE_NAME).write_text(
        manifest.to_json(),
        encoding="utf-8",
        newline="\n",
    )


def replace_file_entry(manifest, name: str, payload: bytes):
    files = tuple(
        replace(
            entry,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        if entry.path == name
        else entry
        for entry in manifest.files
    )
    return replace(manifest, files=files, manifest_hash=None)


def make_policy(**changes):
    values = {
        "policy_id": "policy:restoration-001",
        "reject_symlink_components": True,
        "require_exact_entry_set": True,
        "require_canonical_payloads": True,
        "require_compatible_checkpoint": True,
        "max_file_bytes": 64 * 1024 * 1024,
    }
    values.update(changes)
    return ArtifactOrchestrationRestorationPolicy(**values)


def make_request(context, policy=None, **changes):
    effective_policy = policy or make_policy()
    values = {
        "persistence_id": context["persistence_id"],
        "state_root": context["root"],
        "policy": effective_policy,
        "requested_by": "ELMAN_NEXUS",
        "requested_at": RESTORED_AT,
        "expected_manifest_hash": context["manifest"].manifest_hash,
        "expected_orchestration_result_hash": (
            context["manifest"].orchestration_result_hash
        ),
    }
    values.update(changes)
    return ArtifactOrchestrationRestorationRequest.from_identifiers(
        **values
    )


def make_restoration(context, policy=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(
        context,
        policy=effective_policy,
        **request_changes,
    )
    return ArtifactOrchestrationStateRestoration(
        request,
        effective_policy,
    )


def snapshot_tree(root: Path):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            snapshot[relative] = path.read_bytes()
        else:
            snapshot[relative] = None
    return snapshot


class RestorationPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(max_file_bytes=4096)
        self.assertEqual(
            ArtifactOrchestrationRestorationPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_rejects_non_boolean(self):
        with self.assertRaises(ArtifactOrchestrationRestorationError):
            make_policy(require_exact_entry_set="yes")

    def test_policy_rejects_zero_file_limit(self):
        with self.assertRaises(ArtifactOrchestrationRestorationError):
            make_policy(max_file_bytes=0)

    def test_policy_rejects_unsupported_version(self):
        with self.assertRaises(ArtifactOrchestrationRestorationError):
            ArtifactOrchestrationRestorationPolicy(
                policy_id="policy:restoration-001",
                version=2,
            )

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class RestorationRequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            first = make_request(context)
            second = make_request(context)
            self.assertEqual(first.restoration_id, second.restoration_id)

    def test_request_identifier_changes_with_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_context = write_state(base / "one")
            second_context = write_state(base / "two")
            self.assertNotEqual(
                make_request(first_context).restoration_id,
                make_request(second_context).restoration_id,
            )

    def test_request_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            request = make_request(context)
            restored = ArtifactOrchestrationRestorationRequest.from_json(
                request.to_json()
            )
            self.assertEqual(restored, request)
            restored.verify_hash()

    def test_request_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            data = make_request(context).to_dict()
            data["requested_by"] = "ELMAN_CORE"
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestorationRequest.from_dict(data)

    def test_request_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            data = make_request(context).to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestorationRequest.from_dict(data)

    def test_request_rejects_relative_root(self):
        policy = make_policy()
        with self.assertRaises(ArtifactOrchestrationRestorationError):
            ArtifactOrchestrationRestorationRequest.from_identifiers(
                persistence_id=PERSISTENCE_ID,
                state_root="relative/state",
                policy=policy,
                requested_by="ELMAN_NEXUS",
                requested_at=RESTORED_AT,
            )

    def test_request_rejects_non_utc_datetime(self):
        policy = make_policy()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactOrchestrationRestorationError):
                ArtifactOrchestrationRestorationRequest.from_identifiers(
                    persistence_id=PERSISTENCE_ID,
                    state_root=Path(directory),
                    policy=policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_accepts_utc_datetime(self):
        policy = make_policy()
        with tempfile.TemporaryDirectory() as directory:
            request = (
                ArtifactOrchestrationRestorationRequest.from_identifiers(
                    persistence_id=PERSISTENCE_ID,
                    state_root=Path(directory),
                    policy=policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=datetime(2026, 8, 5, tzinfo=UTC),
                )
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_allows_unbound_expectations(self):
        policy = make_policy()
        with tempfile.TemporaryDirectory() as directory:
            request = (
                ArtifactOrchestrationRestorationRequest.from_identifiers(
                    persistence_id=PERSISTENCE_ID,
                    state_root=Path(directory),
                    policy=policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=RESTORED_AT,
                )
            )
            self.assertIsNone(request.expected_manifest_hash)
            self.assertIsNone(
                request.expected_orchestration_result_hash
            )

    def test_request_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            request = make_request(context)
            with self.assertRaises(FrozenInstanceError):
                request.persistence_id = "other:id"  # type: ignore[misc]


class RestorationExecutionTests(unittest.TestCase):
    def test_restore_returns_restored_status(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            self.assertIs(
                result.status,
                ArtifactOrchestrationRestorationStatus.RESTORED,
            )

    def test_restore_reconstructs_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            self.assertEqual(result.restored_state.plan, context["plan"])

    def test_restore_reconstructs_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            self.assertEqual(
                result.restored_state.journal.to_jsonl(),
                context["journal"].to_jsonl(),
            )

    def test_restore_reconstructs_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            self.assertEqual(
                result.restored_state.checkpoint,
                context["checkpoint"],
            )

    def test_restore_links_manifest_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            self.assertEqual(
                result.manifest_hash,
                context["manifest"].manifest_hash,
            )

    def test_state_directory_key_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            restoration = make_restoration(context)
            self.assertEqual(
                restoration.state_directory.name,
                hashlib.sha256(
                    context["persistence_id"].encode("utf-8")
                ).hexdigest(),
            )

    def test_completed_state_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="completed")
            state = make_restoration(context).restore().restored_state
            self.assertIs(
                state.assessment_status,
                ResumeAssessmentStatus.TERMINAL,
            )
            self.assertFalse(state.can_resume)

    def test_running_state_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="running")
            state = make_restoration(context).restore().restored_state
            self.assertIs(
                state.assessment_status,
                ResumeAssessmentStatus.READY,
            )
            self.assertTrue(state.can_resume)

    def test_blocked_state_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="blocked")
            state = make_restoration(context).restore().restored_state
            self.assertIs(
                state.assessment_status,
                ResumeAssessmentStatus.BLOCKED,
            )
            self.assertFalse(state.can_resume)

    def test_restore_does_not_modify_state(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            before = snapshot_tree(context["root"])
            make_restoration(context).restore()
            after = snapshot_tree(context["root"])
            self.assertEqual(after, before)

    def test_restore_creates_no_new_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            before = {
                path.relative_to(context["root"]).as_posix()
                for path in context["root"].rglob("*")
            }
            make_restoration(context).restore()
            after = {
                path.relative_to(context["root"]).as_posix()
                for path in context["root"].rglob("*")
            }
            self.assertEqual(after, before)

    def test_restoration_object_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            restoration = make_restoration(context)
            with self.assertRaises(FrozenInstanceError):
                restoration.policy = make_policy()  # type: ignore[misc]

    def test_constructor_rejects_policy_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            request = make_request(context, policy=make_policy())
            other = make_policy(policy_id="policy:restoration-002")
            with self.assertRaises(ArtifactOrchestrationRestorationError):
                ArtifactOrchestrationStateRestoration(request, other)


class RestorationBindingTests(unittest.TestCase):
    def test_expected_manifest_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(
                    context,
                    expected_manifest_hash=sha("wrong"),
                ).restore()

    def test_expected_orchestration_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(
                    context,
                    expected_orchestration_result_hash=sha("wrong"),
                ).restore()

    def test_manifest_persistence_id_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                persistence_id="orchestration-persistence:" + "b" * 64,
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_manifest_plan_id_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                plan_id="plan:other",
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_manifest_project_id_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                project_id="project:other",
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_manifest_plan_state_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                result_plan_state_hash=sha("wrong"),
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_manifest_journal_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                result_journal_hash=sha("wrong"),
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_manifest_checkpoint_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = replace(
                context["manifest"],
                result_checkpoint_hash=sha("wrong"),
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()


class RestorationFilesystemSafetyTests(unittest.TestCase):
    def test_missing_state_root_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory) / "state")
            missing = Path(directory) / "missing"
            request = ArtifactOrchestrationRestorationRequest.from_identifiers(
                persistence_id=context["persistence_id"],
                state_root=missing,
                policy=make_policy(),
                requested_by="ELMAN_NEXUS",
                requested_at=RESTORED_AT,
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationNotFoundError
            ):
                ArtifactOrchestrationStateRestoration(
                    request,
                    make_policy(),
                ).restore()

    def test_missing_state_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = ArtifactOrchestrationRestorationRequest.from_identifiers(
                persistence_id=PERSISTENCE_ID,
                state_root=root,
                policy=make_policy(),
                requested_by="ELMAN_NEXUS",
                requested_at=RESTORED_AT,
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationNotFoundError
            ):
                ArtifactOrchestrationStateRestoration(
                    request,
                    make_policy(),
                ).restore()

    def test_missing_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / MANIFEST_FILE_NAME).unlink()
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(
                    context,
                    expected_manifest_hash=None,
                ).restore()

    def test_extra_file_is_rejected_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_extra_file_can_be_allowed_by_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            policy = make_policy(require_exact_entry_set=False)
            result = make_restoration(context, policy=policy).restore()
            self.assertIs(
                result.status,
                ArtifactOrchestrationRestorationStatus.RESTORED,
            )

    def test_symlink_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            target = context["state_directory"] / PLAN_FILE_NAME
            backup = context["state_directory"] / "plan.backup"
            target.rename(backup)
            try:
                os.symlink(backup, target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_symlink_component_check_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            with patch(
                "elman_os.artifact_orchestration_state_restoration."
                "_reject_symlink_components",
                side_effect=(
                    ArtifactOrchestrationRestorationIntegrityError(
                        "symlink path component is forbidden"
                    )
                ),
            ):
                with self.assertRaises(
                    ArtifactOrchestrationRestorationIntegrityError
                ):
                    make_restoration(context).restore()

    def test_oversized_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            policy = make_policy(max_file_bytes=16)
            with self.assertRaises(
                ArtifactOrchestrationRestorationReadError
            ):
                make_restoration(context, policy=policy).restore()

    def test_oversized_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["state_directory"] / JOURNAL_FILE_NAME
            payload = path.read_bytes() + b"\n" + b" " * 10000
            path.write_bytes(payload)
            manifest = replace_file_entry(
                context["manifest"],
                JOURNAL_FILE_NAME,
                payload,
            )
            rewrite_manifest(context, manifest)
            manifest_size = (
                context["state_directory"] / MANIFEST_FILE_NAME
            ).stat().st_size
            policy = make_policy(
                require_canonical_payloads=False,
                max_file_bytes=manifest_size + 64,
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationReadError
            ):
                make_restoration(context, policy=policy).restore()


class RestorationTamperTests(unittest.TestCase):
    def test_tampered_plan_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / PLAN_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_tampered_journal_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / JOURNAL_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_tampered_checkpoint_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            (context["state_directory"] / CHECKPOINT_FILE_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_tampered_manifest_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["state_directory"] / MANIFEST_FILE_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            data["project_id"] = "project:tampered"
            path.write_text(canonical_json(data), encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(
                    context,
                    expected_manifest_hash=None,
                ).restore()

    def test_noncanonical_plan_is_rejected_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["state_directory"] / PLAN_FILE_NAME
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
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()

    def test_noncanonical_plan_can_be_read_when_policy_allows_it(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            path = context["state_directory"] / PLAN_FILE_NAME
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
            result = make_restoration(context, policy=policy).restore()
            self.assertTrue(
                result.restored_state.plan_json.endswith("\n")
            )

    def test_invalid_plan_json_is_rejected_even_with_updated_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            payload = b"{invalid"
            path = context["state_directory"] / PLAN_FILE_NAME
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
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context, policy=policy).restore()

    def test_incompatible_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory), kind="completed")
            other_plan, other_journal, other_checkpoint = (
                make_execution_state("running")
            )
            other_checkpoint = replace(
                other_checkpoint,
                plan_id="plan:other",
                checkpoint_hash=None,
            )
            payload = other_checkpoint.to_json().encode("utf-8")
            path = context["state_directory"] / CHECKPOINT_FILE_NAME
            path.write_bytes(payload)
            manifest = replace_file_entry(
                context["manifest"],
                CHECKPOINT_FILE_NAME,
                payload,
            )
            manifest = replace(
                manifest,
                result_checkpoint_hash=other_checkpoint.checkpoint_hash,
                manifest_hash=None,
            )
            rewrite_manifest(context, manifest)
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                make_restoration(context).restore()


class RestorationSerializationTests(unittest.TestCase):
    def test_restored_state_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            state = make_restoration(context).restore().restored_state
            restored = ArtifactOrchestrationRestoredState.from_json(
                state.to_json()
            )
            self.assertEqual(restored, state)
            restored.verify_hash()

    def test_restored_state_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            state = make_restoration(context).restore().restored_state
            data = state.to_dict()
            data["project_id"] = "project:other"
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestoredState.from_dict(data)

    def test_restored_state_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            state = make_restoration(context).restore().restored_state
            data = state.to_dict()
            del data["state_hash"]
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestoredState.from_dict(data)

    def test_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            restored = ArtifactOrchestrationRestorationResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)
            restored.verify_hash()

    def test_result_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestorationResult.from_dict(data)

    def test_result_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestorationResult.from_dict(data)

    def test_result_rejects_state_directory_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            data = result.to_dict()
            data["state_directory"] = Path(directory).parent.as_posix()
            with self.assertRaises(
                ArtifactOrchestrationRestorationIntegrityError
            ):
                ArtifactOrchestrationRestorationResult.from_dict(data)

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            result = make_restoration(context).restore()
            with self.assertRaises(FrozenInstanceError):
                result.reason = "other"  # type: ignore[misc]


class PersistenceResultBindingTests(unittest.TestCase):
    def test_request_from_persistence_result_binds_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            context = write_state(Path(directory))
            manifest = context["manifest"]
            manifest_hash = manifest.manifest_hash
            assert manifest_hash is not None
            persistence_result = ArtifactOrchestrationPersistenceResult(
                persistence_id=context["persistence_id"],
                status=ArtifactOrchestrationPersistenceStatus.PERSISTED,
                request_hash=manifest.request_hash,
                policy_id=manifest.policy_id,
                policy_hash=manifest.policy_hash,
                orchestration_id=manifest.orchestration_id,
                orchestration_result_hash=(
                    manifest.orchestration_result_hash
                ),
                state_root=context["root"].as_posix(),
                state_directory=context["state_directory"].as_posix(),
                manifest_hash=manifest_hash,
                manifest_json=manifest.to_json(),
                completed_at=PERSISTED_AT,
                reason="PERSISTED: test fixture",
            )
            policy = make_policy()
            request = (
                ArtifactOrchestrationRestorationRequest
                .from_persistence_result(
                    persistence_result,
                    policy,
                    requested_by="ELMAN_NEXUS",
                    requested_at=RESTORED_AT,
                )
            )
            self.assertEqual(
                request.expected_manifest_hash,
                manifest_hash,
            )
            self.assertEqual(
                request.expected_orchestration_result_hash,
                manifest.orchestration_result_hash,
            )


if __name__ == "__main__":
    unittest.main()
