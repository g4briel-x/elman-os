from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from elman_os.artifact_orchestration_selected_state_resume_persistence import (
    ArtifactOrchestrationSelectedStateResumePersistence,
    ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError,
    ArtifactOrchestrationSelectedStateResumePersistenceError,
    ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError,
    ArtifactOrchestrationSelectedStateResumePersistenceLockError,
    ArtifactOrchestrationSelectedStateResumePersistencePolicy,
    ArtifactOrchestrationSelectedStateResumePersistenceRequest,
    ArtifactOrchestrationSelectedStateResumePersistenceResult,
    ArtifactOrchestrationSelectedStateResumePersistenceStatus,
)
from elman_os.artifact_orchestration_state_persistence import (
    CHECKPOINT_FILE_NAME,
    JOURNAL_FILE_NAME,
    MANIFEST_FILE_NAME,
    PLAN_FILE_NAME,
    ArtifactOrchestrationPersistencePolicy,
    ArtifactOrchestrationPersistenceStatus,
)
from elman_os.artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationPolicy,
    ArtifactOrchestrationRestorationRequest,
    ArtifactOrchestrationStateRestoration,
)
from test_artifact_orchestration_selected_state_resume_application import (
    apply_authorization,
)


PERSISTED_AT = "2026-08-06T01:03:00Z"
RESTORED_AT = "2026-08-06T01:04:00Z"


def make_policy(**changes):
    persistence_policy = changes.pop(
        "persistence_policy",
        ArtifactOrchestrationPersistencePolicy(
            policy_id="policy:selected-state-resume-storage",
            fsync_files=False,
            require_directory_fsync=False,
            reject_symlink_components=True,
            max_file_bytes=64 * 1024 * 1024,
        ),
    )
    values = {
        "policy_id": "policy:selected-state-resume-persistence",
        "persistence_policy": persistence_policy,
        "require_successful_application": True,
        "require_new_persistence_id": True,
        "require_source_immutability": True,
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateResumePersistencePolicy.from_persistence_policy(
        **values
    )


def make_request(root: Path, application=None, policy=None, **changes):
    effective_application = application or apply_authorization()
    effective_policy = policy or make_policy()
    values = {
        "application_result": effective_application,
        "policy": effective_policy,
        "state_root": root,
        "requested_by": "ELMAN_NEXUS",
        "requested_at": PERSISTED_AT,
        "rationale": "Persist the verified copy-on-write resume state.",
    }
    values.update(changes)
    return ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_application_result(
        **values
    )


def persist(root: Path, application=None, policy=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(
        root,
        application=application,
        policy=effective_policy,
        **request_changes,
    )
    boundary = ArtifactOrchestrationSelectedStateResumePersistence(
        request,
        effective_policy,
    )
    return boundary.persist(), boundary


class SelectedStateResumePersistencePolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy()
        restored = ArtifactOrchestrationSelectedStateResumePersistencePolicy.from_json(
            policy.to_json()
        )
        self.assertEqual(restored, policy)
        self.assertEqual(restored.persistence_policy, policy.persistence_policy)

    def test_policy_rejects_disabled_application_requirement(self):
        with self.assertRaises(ArtifactOrchestrationSelectedStateResumePersistenceError):
            make_policy(require_successful_application=False)

    def test_policy_rejects_destination_reuse(self):
        with self.assertRaises(ArtifactOrchestrationSelectedStateResumePersistenceError):
            make_policy(require_new_persistence_id=False)

    def test_policy_rejects_disabled_source_immutability(self):
        with self.assertRaises(ArtifactOrchestrationSelectedStateResumePersistenceError):
            make_policy(require_source_immutability=False)

    def test_policy_rejects_tampered_embedded_policy_hash(self):
        policy = make_policy()
        data = policy.to_dict()
        data["persistence_policy_hash"] = "0" * 64
        with self.assertRaises(
            ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
        ):
            ArtifactOrchestrationSelectedStateResumePersistencePolicy.from_dict(data)

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class SelectedStateResumePersistenceRequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = apply_authorization()
            self.assertEqual(
                make_request(root, application=application).persistence_request_id,
                make_request(root, application=application).persistence_request_id,
            )

    def test_request_derives_new_destination_and_checkpoint_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory))
            self.assertNotEqual(request.persistence_id, request.source_persistence_id)
            self.assertTrue(request.persistence_id.startswith("resume-state:"))
            self.assertTrue(request.checkpoint_id.startswith("resume-checkpoint:"))

    def test_request_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory))
            restored = ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_json(
                request.to_json()
            )
            self.assertEqual(restored, request)
            restored.verify_hash()

    def test_request_rejects_time_before_application(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError
            ):
                make_request(
                    Path(directory),
                    requested_at="2026-08-06T01:00:00Z",
                )

    def test_request_rejects_relative_root(self):
        with self.assertRaises(ArtifactOrchestrationSelectedStateResumePersistenceError):
            make_request(Path("relative-state-root"))

    def test_request_rejects_source_identifier_as_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            application = apply_authorization()
            source_id = (
                application.application_request.authorization_result
                .authorization_request.restoration_result.restored_state.persistence_id
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceAuthorizationError
            ):
                make_request(
                    Path(directory),
                    application=application,
                    persistence_id=source_id,
                )

    def test_request_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory))
            data = request.to_dict()
            data["rationale"] = "Tampered"
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_dict(data)

    def test_request_rejects_tampered_application_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory))
            data = request.to_dict()
            data["application_result_hash"] = "0" * 64
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_dict(data)

    def test_request_rejects_missing_request_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            data = make_request(Path(directory)).to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_dict(data)

    def test_request_factory_rejects_wrong_application_type(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_application_result(
                    application_result="invalid",  # type: ignore[arg-type]
                    policy=make_policy(),
                    state_root=Path(directory),
                    requested_by="ELMAN_NEXUS",
                    requested_at=PERSISTED_AT,
                    rationale="Persist",
                )


class SelectedStateResumePersistenceExecutionTests(unittest.TestCase):
    def test_persist_writes_new_immutable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            result, boundary = persist(Path(directory))
            self.assertIs(
                result.status,
                ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED,
            )
            self.assertTrue(boundary.state_directory.is_dir())
            self.assertEqual(
                {item.name for item in boundary.state_directory.iterdir()},
                {
                    PLAN_FILE_NAME,
                    JOURNAL_FILE_NAME,
                    CHECKPOINT_FILE_NAME,
                    MANIFEST_FILE_NAME,
                },
            )

    def test_persist_captures_fresh_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            request = result.persistence_request
            checkpoint = result.checkpoint
            application = request.application_result
            self.assertEqual(checkpoint.checkpoint_id, request.checkpoint_id)
            self.assertEqual(checkpoint.plan_id, application.updated_plan.plan_id)
            self.assertEqual(
                checkpoint.journal_event_count,
                application.updated_journal.event_count,
            )
            checkpoint.verify_hash()

    def test_manifest_binds_application_and_new_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            manifest = result.persistence_result.manifest
            request = result.persistence_request
            self.assertEqual(
                manifest.orchestration_result_hash,
                request.application_result_hash,
            )
            self.assertEqual(manifest.result_checkpoint_hash, result.checkpoint_hash)
            self.assertEqual(manifest.persistence_id, request.persistence_id)
            self.assertNotEqual(manifest.persistence_id, request.source_persistence_id)

    def test_source_restored_state_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            application = apply_authorization()
            source = (
                application.application_request.authorization_result
                .authorization_request.restoration_result.restored_state
            )
            before = {
                "source": source.to_json(),
                "plan": source.plan.to_json(),
                "journal": source.journal.to_jsonl(),
                "checkpoint": source.checkpoint.to_json(),
            }
            persist(Path(directory), application=application)
            after = {
                "source": source.to_json(),
                "plan": source.plan.to_json(),
                "journal": source.journal.to_jsonl(),
                "checkpoint": source.checkpoint.to_json(),
            }
            self.assertEqual(after, before)

    def test_repeated_persistence_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = apply_authorization()
            first, _ = persist(root, application=application)
            second, _ = persist(root, application=application)
            self.assertIs(
                first.status,
                ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED,
            )
            self.assertIs(
                second.status,
                ArtifactOrchestrationSelectedStateResumePersistenceStatus.NOOP,
            )
            self.assertIs(
                second.persistence_result.status,
                ArtifactOrchestrationPersistenceStatus.NOOP,
            )

    def test_divergent_final_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = apply_authorization()
            _, boundary = persist(root, application=application)
            plan_path = boundary.state_directory / PLAN_FILE_NAME
            plan_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                persist(root, application=application)

    def test_existing_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = make_policy()
            request = make_request(root, policy=policy)
            boundary = ArtifactOrchestrationSelectedStateResumePersistence(
                request,
                policy,
            )
            boundary.lock_path.parent.mkdir(parents=True)
            boundary.lock_path.write_text("occupied", encoding="utf-8")
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceLockError
            ):
                boundary.persist()

    def test_file_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            tiny = ArtifactOrchestrationPersistencePolicy(
                policy_id="policy:tiny-resume-storage",
                fsync_files=False,
                require_directory_fsync=False,
                reject_symlink_components=True,
                max_file_bytes=1,
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceError
            ):
                persist(Path(directory), policy=make_policy(persistence_policy=tiny))

    def test_complete_staging_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = apply_authorization()
            _, boundary = persist(root, application=application)
            boundary.staging_directory.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(
                boundary.state_directory.as_posix(),
                boundary.staging_directory.as_posix(),
            )
            recovered, _ = persist(root, application=application)
            self.assertIs(
                recovered.status,
                ArtifactOrchestrationSelectedStateResumePersistenceStatus.PERSISTED,
            )
            self.assertTrue(boundary.state_directory.exists())
            self.assertFalse(boundary.staging_directory.exists())

    def test_persisted_state_is_restorable_by_standard_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _ = persist(root)
            persistence = result.persistence_result
            restoration_policy = ArtifactOrchestrationRestorationPolicy(
                policy_id="policy:resume-state-restoration",
                reject_symlink_components=True,
                require_exact_entry_set=True,
                require_canonical_payloads=True,
                require_compatible_checkpoint=True,
                max_file_bytes=64 * 1024 * 1024,
            )
            restoration_request = ArtifactOrchestrationRestorationRequest.from_identifiers(
                persistence_id=persistence.persistence_id,
                state_root=root,
                policy=restoration_policy,
                requested_by="ELMAN_NEXUS",
                requested_at=RESTORED_AT,
                expected_manifest_hash=persistence.manifest_hash,
                expected_orchestration_result_hash=(
                    result.persistence_request.application_result_hash
                ),
            )
            restored = ArtifactOrchestrationStateRestoration(
                restoration_request,
                restoration_policy,
            ).restore()
            self.assertEqual(
                restored.restored_state.persistence_id,
                persistence.persistence_id,
            )
            self.assertEqual(
                restored.restored_state.checkpoint_hash,
                result.checkpoint_hash,
            )

    def test_constructor_rejects_different_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory), policy=make_policy())
            other = make_policy(
                persistence_policy=ArtifactOrchestrationPersistencePolicy(
                    policy_id="policy:other-storage",
                    fsync_files=False,
                    require_directory_fsync=False,
                    reject_symlink_components=True,
                    max_file_bytes=4096,
                )
            )
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceError
            ):
                ArtifactOrchestrationSelectedStateResumePersistence(request, other)


class SelectedStateResumePersistenceResultTests(unittest.TestCase):
    def test_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            restored = ArtifactOrchestrationSelectedStateResumePersistenceResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)
            restored.verify_hash()

    def test_result_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            data = result.to_dict()
            data["reason"] = "Tampered"
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceResult.from_dict(data)

    def test_result_rejects_tampered_checkpoint_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            data = result.to_dict()
            data["checkpoint_hash"] = "0" * 64
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceResult.from_dict(data)

    def test_result_rejects_status_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            data = result.to_dict()
            data["status"] = "noop"
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceResult.from_dict(data)

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            with self.assertRaises(FrozenInstanceError):
                result.reason = "other"  # type: ignore[misc]

    def test_serialized_request_rejects_modified_source_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(Path(directory))
            data = request.to_dict()
            data["source_persistence_id"] = "orchestration-persistence:" + "0" * 64
            data["request_hash"] = request.request_hash
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceRequest.from_dict(data)

    def test_embedded_persistence_result_is_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = persist(Path(directory))
            data = result.to_dict()
            embedded = json.loads(data["persistence_result_json"])
            embedded["orchestration_result_hash"] = "0" * 64
            data["persistence_result_json"] = json.dumps(embedded)
            with self.assertRaises(
                ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
            ):
                ArtifactOrchestrationSelectedStateResumePersistenceResult.from_dict(data)


if __name__ == "__main__":
    unittest.main()
