from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import math
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest

from elman_os.project_memory import (
    ImmutableDecisionError,
    ProjectMemoryConflictError,
    ProjectMemoryError,
    ProjectMemoryIntegrityError,
    ProjectMemoryKind,
    ProjectMemoryOrigin,
    ProjectMemoryRetentionClass,
    ProjectMemoryRetentionPolicy,
    ProjectMemorySecretError,
    ProjectMemorySourceType,
    ProjectMemoryState,
    ProjectMemoryStore,
)


TENANT = "tenant:elman"
PROJECT = "project:elman-os"
EXECUTION = "execution:v070-memory"
T0 = "2026-08-07T04:00:00Z"
T1 = "2026-08-07T04:01:00Z"
T2 = "2026-08-08T04:00:00Z"


def origin(
    captured_at=T0,
    *,
    source_type=ProjectMemorySourceType.USER_APPROVAL,
    source_id="approval:memory-001",
    actor_id="human:owner-001",
    evidence=("evidence:roadmap-v070",),
):
    return ProjectMemoryOrigin(
        source_type=source_type,
        source_id=source_id,
        actor_id=actor_id,
        captured_at=captured_at,
        evidence_references=evidence,
    )


class ProjectMemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "project-memory.sqlite3"
        self.policy = ProjectMemoryRetentionPolicy(
            policy_id="policy:memory-tests-v1",
            execution_days=2,
            transient_days=1,
            maximum_query_results=100,
        )
        self.store = ProjectMemoryStore(
            self.database_path,
            retention_policy=self.policy,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def record_constraint(self, **changes):
        values = {
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "execution_id": EXECUTION,
            "kind": ProjectMemoryKind.CONSTRAINT,
            "title": "Python-first kernel",
            "content": {
                "rule": "Kernel implementation remains Python-first.",
                "status": "validated",
            },
            "labels": ("architecture", "kernel"),
            "origin": origin(),
            "retention_class": ProjectMemoryRetentionClass.PROJECT,
        }
        values.update(changes)
        return self.store.record(**values)


class ContractTests(ProjectMemoryTestCase):
    def test_origin_round_trip_is_canonical_and_immutable(self):
        first = origin()
        restored = ProjectMemoryOrigin.from_json(first.to_json())
        self.assertEqual(first, restored)
        with self.assertRaises(FrozenInstanceError):
            first.actor_id = "human:other"  # type: ignore[misc]

    def test_origin_requires_utc_and_valid_provenance(self):
        with self.assertRaises(ProjectMemoryError):
            origin(captured_at="2026-08-07T04:00:00+01:00")
        with self.assertRaises(ProjectMemoryError):
            origin(evidence=("not valid with spaces",))

    def test_origin_rejects_credential_material(self):
        with self.assertRaises(ProjectMemorySecretError):
            origin(source_id="ghp_" + "0" * 32)

    def test_retention_policy_is_fail_closed_and_hash_stable(self):
        same = ProjectMemoryRetentionPolicy(
            policy_id=self.policy.policy_id,
            execution_days=2,
            transient_days=1,
            maximum_query_results=100,
        )
        self.assertEqual(self.policy.policy_hash, same.policy_hash)
        with self.assertRaises(ProjectMemoryError):
            ProjectMemoryRetentionPolicy(
                policy_id="policy:unsafe-memory",
                fail_closed=False,
            )

    def test_invalid_identifier_and_non_finite_json_are_rejected(self):
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(project_id="bad project")
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(content={"score": math.nan})

    def test_decisions_require_permanent_retention(self):
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(
                kind=ProjectMemoryKind.DECISION,
                retention_class=ProjectMemoryRetentionClass.PROJECT,
            )

    def test_decisions_require_human_approval_provenance_and_evidence(self):
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(
                kind=ProjectMemoryKind.DECISION,
                retention_class=ProjectMemoryRetentionClass.PERMANENT,
                origin=origin(
                    source_type=ProjectMemorySourceType.SYSTEM,
                    source_id="system:decision-candidate",
                ),
            )
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(
                kind=ProjectMemoryKind.DECISION,
                retention_class=ProjectMemoryRetentionClass.PERMANENT,
                origin=origin(evidence=()),
            )

    def test_execution_retention_requires_execution_identifier(self):
        with self.assertRaises(ProjectMemoryError):
            self.record_constraint(
                execution_id=None,
                retention_class=ProjectMemoryRetentionClass.EXECUTION,
            )

    def test_durable_knowledge_rejects_expiring_retention(self):
        for kind in (
            ProjectMemoryKind.CONSTRAINT,
            ProjectMemoryKind.CONVENTION,
            ProjectMemoryKind.MIGRATION,
            ProjectMemoryKind.SOURCE_OF_TRUTH,
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(ProjectMemoryError):
                    self.record_constraint(
                        kind=kind,
                        retention_class=ProjectMemoryRetentionClass.TRANSIENT,
                    )

    def test_nested_sensitive_key_is_rejected_before_storage(self):
        with self.assertRaises(ProjectMemorySecretError):
            self.record_constraint(
                content={"provider": {"api_key": "not-even-a-real-key"}}
            )
        self.assertEqual(
            self.store.search(tenant_id=TENANT, project_id=PROJECT),
            (),
        )

    def test_known_credential_patterns_are_rejected_before_storage(self):
        forbidden = (
            "ghp_" + "012345678901234567890123456789012345",
            "sk-" + "proj-012345678901234567890123456789",
            "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "-----BEGIN " + "PRIVATE KEY-----",
            "password=correct-horse-battery-staple",
            "eyJabcdefghijk.abcdefghijk.abcdefghijk",
        )
        for index, value in enumerate(forbidden):
            with self.subTest(index=index):
                with self.assertRaises(ProjectMemorySecretError):
                    self.record_constraint(content={"note": value})

    def test_security_discussion_without_credential_material_is_allowed(self):
        record = self.record_constraint(
            title="Credential exclusion rule",
            content={
                "rule": "Credentials must be excluded from project memory.",
                "evidence": "The scanner rejects sensitive field names.",
            },
        )
        self.assertTrue(record.payload_available)


class StorageTests(ProjectMemoryTestCase):
    def test_record_is_hash_bound_and_readable(self):
        record = self.record_constraint()
        record.verify_hash()
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.kind, ProjectMemoryKind.CONSTRAINT)
        self.assertEqual(record.content["status"], "validated")
        self.assertEqual(record.labels, ("architecture", "kernel"))
        restored = self.store.get(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=record.memory_id,
        )
        self.assertEqual(restored, record)
        self.assertEqual(restored.to_json(), record.to_json())
        self.assertEqual(
            type(record).from_json(record.to_json()),
            record,
        )

    def test_duplicate_immutable_record_is_rejected(self):
        self.record_constraint()
        with self.assertRaises(ProjectMemoryConflictError):
            self.record_constraint()

    def test_store_reopens_without_losing_memory(self):
        record = self.record_constraint()
        reopened = ProjectMemoryStore(
            self.database_path,
            retention_policy=self.policy,
        )
        restored = reopened.get(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=record.memory_id,
        )
        self.assertEqual(restored, record)

    def test_tenant_and_project_isolation_are_mandatory(self):
        record = self.record_constraint()
        self.assertIsNone(
            self.store.get(
                tenant_id="tenant:other",
                project_id=PROJECT,
                memory_id=record.memory_id,
            )
        )
        self.assertIsNone(
            self.store.get(
                tenant_id=TENANT,
                project_id="project:other",
                memory_id=record.memory_id,
            )
        )

    def test_database_file_is_owner_only_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX file modes are not available on Windows")
        self.assertEqual(self.database_path.stat().st_mode & 0o777, 0o600)

    def test_revision_history_is_append_only_and_hash_chained(self):
        first = self.record_constraint()
        second = self.store.revise(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=first.memory_id,
            expected_revision=1,
            title="Python-first kernel and tooling",
            content={
                "rule": "Kernel and release tooling remain Python-first.",
                "status": "validated",
            },
            labels=("architecture", "kernel", "tooling"),
            origin=origin(
                T1,
                source_id="approval:memory-002",
                evidence=("evidence:architecture-review",),
            ),
        )
        history = self.store.history(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=first.memory_id,
        )
        self.assertEqual(history, (first, second))
        self.assertEqual(second.previous_revision_hash, first.revision_hash)
        self.assertEqual(history[0].content["rule"], first.content["rule"])

    def test_optimistic_revision_conflict_is_rejected(self):
        first = self.record_constraint()
        self.store.revise(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=first.memory_id,
            expected_revision=1,
            title="Revised constraint",
            content={"rule": "Revised once."},
            origin=origin(T1, source_id="approval:revision-001"),
        )
        with self.assertRaises(ProjectMemoryConflictError):
            self.store.revise(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=first.memory_id,
                expected_revision=1,
                title="Stale revision",
                content={"rule": "This write is stale."},
                origin=origin(T2, source_id="approval:revision-stale"),
            )

    def test_obsolete_revision_is_terminal(self):
        first = self.record_constraint()
        obsolete = self.store.mark_obsolete(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=first.memory_id,
            expected_revision=1,
            origin=origin(T1, source_id="approval:obsolete-001"),
        )
        self.assertEqual(obsolete.state, ProjectMemoryState.OBSOLETE)
        with self.assertRaises(ProjectMemoryConflictError):
            self.store.revise(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=first.memory_id,
                expected_revision=2,
                title="Reactivated",
                content={"rule": "Unsafe silent reactivation."},
                origin=origin(T2, source_id="approval:reactivate-001"),
            )

    def test_decision_cannot_be_revised_in_place(self):
        decision = self.record_constraint(
            kind=ProjectMemoryKind.DECISION,
            title="Adopt SQLite memory",
            content={"decision": "Use local SQLite with immutable revisions."},
            retention_class=ProjectMemoryRetentionClass.PERMANENT,
        )
        with self.assertRaises(ImmutableDecisionError):
            self.store.revise(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=decision.memory_id,
                expected_revision=1,
                title="Altered decision",
                content={"decision": "Replace the historical decision."},
                origin=origin(T1, source_id="approval:decision-edit"),
            )

    def test_decision_can_only_be_replaced_by_superseding_decision(self):
        first = self.record_constraint(
            kind=ProjectMemoryKind.DECISION,
            title="Initial retention policy",
            content={"decision": "Retain execution payloads for 30 days."},
            retention_class=ProjectMemoryRetentionClass.PERMANENT,
        )
        second = self.record_constraint(
            kind=ProjectMemoryKind.DECISION,
            title="Revised retention policy",
            content={"decision": "Retain execution payloads for 90 days."},
            retention_class=ProjectMemoryRetentionClass.PERMANENT,
            origin=origin(T1, source_id="approval:decision-002"),
            supersedes_memory_id=first.memory_id,
        )
        current = self.store.search(tenant_id=TENANT, project_id=PROJECT)
        self.assertIn(second, current)
        self.assertNotIn(first, current)
        all_records = self.store.search(
            tenant_id=TENANT,
            project_id=PROJECT,
            include_superseded=True,
        )
        self.assertIn(first, all_records)

    def test_obsolete_successor_does_not_silently_reactivate_old_memory(self):
        first = self.record_constraint(
            title="Original constraint",
            content={"rule": "Use policy A."},
        )
        successor = self.record_constraint(
            title="Replacement constraint",
            content={"rule": "Use policy B."},
            origin=origin(T1, source_id="approval:replacement-constraint"),
            supersedes_memory_id=first.memory_id,
        )
        self.store.mark_obsolete(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=successor.memory_id,
            expected_revision=1,
            origin=origin(T2, source_id="approval:obsolete-successor"),
        )
        current_ids = {
            record.memory_id
            for record in self.store.search(
                tenant_id=TENANT,
                project_id=PROJECT,
            )
        }
        self.assertNotIn(first.memory_id, current_ids)

    def test_concurrent_revisions_allow_exactly_one_winner(self):
        first = self.record_constraint()
        barrier = Barrier(2)
        results = []

        def revise(index):
            barrier.wait()
            try:
                results.append(
                    self.store.revise(
                        tenant_id=TENANT,
                        project_id=PROJECT,
                        memory_id=first.memory_id,
                        expected_revision=1,
                        title=f"Concurrent revision {index}",
                        content={"rule": f"Concurrent candidate {index}."},
                        origin=origin(
                            T1,
                            source_id=f"approval:concurrent-{index}",
                        ),
                    )
                )
            except ProjectMemoryConflictError as exc:
                results.append(exc)

        threads = [Thread(target=revise, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [value for value in results if not isinstance(value, Exception)]
        conflicts = [value for value in results if isinstance(value, Exception)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(winners[0].revision, 2)


class SearchTests(ProjectMemoryTestCase):
    def test_search_filters_by_execution_and_kind(self):
        constraint = self.record_constraint()
        test_result = self.record_constraint(
            kind=ProjectMemoryKind.TEST_RESULT,
            title="Kernel suite passed",
            content={"tests": 1790, "result": "passed"},
            labels=("tests",),
            origin=origin(
                T1,
                source_type=ProjectMemorySourceType.TEST_RUN,
                source_id="test-run:1790",
                actor_id="agent:proof",
            ),
            retention_class=ProjectMemoryRetentionClass.EXECUTION,
        )
        other_execution = self.record_constraint(
            execution_id="execution:other",
            kind=ProjectMemoryKind.TEST_RESULT,
            title="Other suite passed",
            content={"tests": 12, "result": "passed"},
            labels=("tests",),
            origin=origin(
                T2,
                source_type=ProjectMemorySourceType.TEST_RUN,
                source_id="test-run:other",
                actor_id="agent:proof",
            ),
            retention_class=ProjectMemoryRetentionClass.EXECUTION,
        )
        found = self.store.search(
            tenant_id=TENANT,
            project_id=PROJECT,
            execution_id=EXECUTION,
            kinds=(ProjectMemoryKind.TEST_RESULT,),
        )
        self.assertEqual(found, (test_result,))
        self.assertNotIn(constraint, found)
        self.assertNotIn(other_execution, found)

    def test_search_matches_title_content_and_labels(self):
        record = self.record_constraint()
        for query in ("python-first", "validated", "architecture"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.store.search(
                        tenant_id=TENANT,
                        project_id=PROJECT,
                        query=query,
                    ),
                    (record,),
                )

    def test_obsolete_memory_is_hidden_by_default(self):
        first = self.record_constraint()
        obsolete = self.store.mark_obsolete(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=first.memory_id,
            expected_revision=1,
            origin=origin(T1, source_id="approval:obsolete-search"),
        )
        self.assertEqual(
            self.store.search(tenant_id=TENANT, project_id=PROJECT),
            (),
        )
        self.assertEqual(
            self.store.search(
                tenant_id=TENANT,
                project_id=PROJECT,
                include_inactive=True,
            ),
            (obsolete,),
        )

    def test_search_query_is_parameterized_and_wildcards_are_literal(self):
        self.record_constraint()
        for query in ("' OR 1=1 --", "%", "_"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.store.search(
                        tenant_id=TENANT,
                        project_id=PROJECT,
                        query=query,
                    ),
                    (),
                )

    def test_query_limit_is_policy_bounded(self):
        with self.assertRaises(ProjectMemoryError):
            self.store.search(
                tenant_id=TENANT,
                project_id=PROJECT,
                limit=101,
            )


class RetentionTests(ProjectMemoryTestCase):
    def test_transient_payload_is_purged_but_metadata_remains_verifiable(self):
        record = self.record_constraint(
            kind=ProjectMemoryKind.TEST_RESULT,
            retention_class=ProjectMemoryRetentionClass.TRANSIENT,
        )
        report = self.store.apply_retention(tenant_id=TENANT, as_of=T2)
        self.assertEqual(report.purged_payload_count, 1)
        self.assertEqual(report.events[0].payload_hash, record.payload_hash)
        self.assertEqual(report.to_json(), report.to_json())
        retained = self.store.get(
            tenant_id=TENANT,
            project_id=PROJECT,
            memory_id=record.memory_id,
        )
        self.assertIsNotNone(retained)
        self.assertFalse(retained.payload_available)
        self.assertIsNone(retained.content)
        retained.verify_hash()

    def test_permanent_and_project_payloads_are_not_purged(self):
        project_record = self.record_constraint()
        permanent = self.record_constraint(
            kind=ProjectMemoryKind.DECISION,
            title="Permanent decision",
            content={"decision": "Retain this decision permanently."},
            origin=origin(T1, source_id="approval:permanent-001"),
            retention_class=ProjectMemoryRetentionClass.PERMANENT,
        )
        report = self.store.apply_retention(
            tenant_id=TENANT,
            as_of="2036-08-07T04:00:00Z",
        )
        self.assertEqual(report.purged_payload_count, 0)
        self.assertTrue(
            self.store.get(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=project_record.memory_id,
            ).payload_available
        )
        self.assertTrue(
            self.store.get(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=permanent.memory_id,
            ).payload_available
        )

    def test_execution_payload_uses_configured_duration(self):
        record = self.record_constraint(
            kind=ProjectMemoryKind.TEST_RESULT,
            retention_class=ProjectMemoryRetentionClass.EXECUTION,
        )
        early = self.store.apply_retention(
            tenant_id=TENANT,
            as_of="2026-08-08T04:00:00Z",
        )
        self.assertEqual(early.purged_payload_count, 0)
        due = self.store.apply_retention(
            tenant_id=TENANT,
            as_of="2026-08-09T04:00:00Z",
        )
        self.assertEqual(due.purged_payload_count, 1)
        self.assertEqual(due.events[0].memory_id, record.memory_id)

    def test_retention_is_idempotent_and_events_are_persistent(self):
        self.record_constraint(
            kind=ProjectMemoryKind.TEST_RESULT,
            retention_class=ProjectMemoryRetentionClass.TRANSIENT,
        )
        first = self.store.apply_retention(tenant_id=TENANT, as_of=T2)
        second = self.store.apply_retention(tenant_id=TENANT, as_of=T2)
        self.assertEqual(first.purged_payload_count, 1)
        self.assertEqual(second.purged_payload_count, 0)
        events = self.store.retention_events(
            tenant_id=TENANT,
            project_id=PROJECT,
        )
        self.assertEqual(events, first.events)
        events[0].verify_hash()

    def test_purged_payload_no_longer_matches_full_text_search(self):
        record = self.record_constraint(
            kind=ProjectMemoryKind.TEST_RESULT,
            content={"unique_marker": "needle-for-retention-search"},
            retention_class=ProjectMemoryRetentionClass.TRANSIENT,
        )
        self.assertEqual(
            self.store.search(
                tenant_id=TENANT,
                project_id=PROJECT,
                query="needle-for-retention-search",
            ),
            (record,),
        )
        self.store.apply_retention(tenant_id=TENANT, as_of=T2)
        self.assertEqual(
            self.store.search(
                tenant_id=TENANT,
                project_id=PROJECT,
                query="needle-for-retention-search",
            ),
            (),
        )


class IntegrityTests(ProjectMemoryTestCase):
    def test_database_trigger_rejects_revision_update_and_delete(self):
        record = self.record_constraint()
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE project_memory_revisions SET state = 'obsolete'
                    WHERE memory_id = ?
                    """,
                    (record.memory_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM project_memory_revisions WHERE memory_id = ?",
                    (record.memory_id,),
                )
        finally:
            connection.close()

    def test_tampered_revision_metadata_is_detected_on_read(self):
        record = self.record_constraint()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP TRIGGER project_memory_revisions_no_update")
            connection.execute(
                """
                UPDATE project_memory_revisions
                SET recorded_at = '2026-08-07T05:00:00.000000Z'
                WHERE memory_id = ?
                """,
                (record.memory_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ProjectMemoryIntegrityError):
            self.store.get(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=record.memory_id,
            )

    def test_tampered_payload_is_detected_on_read(self):
        record = self.record_constraint()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP TRIGGER project_memory_payloads_no_update")
            connection.execute(
                """
                UPDATE project_memory_payloads
                SET content_json = '{"rule":"tampered"}'
                WHERE memory_id = ?
                """,
                (record.memory_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ProjectMemoryIntegrityError):
            self.store.get(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=record.memory_id,
            )

    def test_payload_deletion_without_retention_event_is_detected(self):
        record = self.record_constraint()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "DELETE FROM project_memory_payloads WHERE memory_id = ?",
                (record.memory_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ProjectMemoryIntegrityError):
            self.store.get(
                tenant_id=TENANT,
                project_id=PROJECT,
                memory_id=record.memory_id,
            )


if __name__ == "__main__":
    unittest.main()
