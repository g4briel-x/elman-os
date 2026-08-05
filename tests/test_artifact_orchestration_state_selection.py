from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from elman_os.artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndexEntry,
    ArtifactOrchestrationStateIndexEntryStatus,
    ArtifactOrchestrationStateIndexSnapshot,
)
from elman_os.artifact_orchestration_state_selection import (
    ArtifactOrchestrationStateSelectionError,
    ArtifactOrchestrationStateSelectionIntegrityError,
    ArtifactOrchestrationStateSelectionLimitError,
    ArtifactOrchestrationStateSelectionPolicy,
    ArtifactOrchestrationStateSelectionRecord,
    ArtifactOrchestrationStateSelectionRecordDecision,
    ArtifactOrchestrationStateSelectionRequest,
    ArtifactOrchestrationStateSelectionResult,
    ArtifactOrchestrationStateSelectionStatus,
    ArtifactOrchestrationStateSelectionStrategy,
    ArtifactOrchestrationStateSelector,
)
from elman_os.execution_checkpoint import ResumeAssessmentStatus


INDEXED_AT = "2026-08-05T04:20:00Z"
REQUESTED_AT = "2026-08-05T04:30:00Z"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_valid_entry(
    root: Path,
    *,
    suffix: str,
    persisted_at: str,
    project_id: str = "project:alpha",
    plan_id: str | None = None,
    checkpoint_id: str | None = None,
    assessment_status: ResumeAssessmentStatus = (
        ResumeAssessmentStatus.READY
    ),
    can_resume: bool = True,
) -> ArtifactOrchestrationStateIndexEntry:
    persistence_id = f"orchestration-persistence:{sha(suffix)}"
    storage_key = sha(persistence_id)
    return ArtifactOrchestrationStateIndexEntry(
        storage_key=storage_key,
        status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
        state_directory=(root / storage_key).as_posix(),
        reason_code="verified-state",
        reason="VALID: verified state",
        persistence_id=persistence_id,
        manifest_hash=sha(f"manifest:{suffix}"),
        orchestration_result_hash=sha(f"result:{suffix}"),
        plan_id=plan_id or f"plan:{suffix}",
        project_id=project_id,
        checkpoint_id=checkpoint_id or f"checkpoint:{suffix}",
        assessment_status=assessment_status,
        can_resume=can_resume,
        persisted_at=persisted_at,
        state_hash=sha(f"state:{suffix}"),
    )


def make_nonvalid_entry(
    root: Path,
    *,
    suffix: str,
    status: ArtifactOrchestrationStateIndexEntryStatus = (
        ArtifactOrchestrationStateIndexEntryStatus.ALTERED
    ),
) -> ArtifactOrchestrationStateIndexEntry:
    storage_key = sha(f"invalid:{suffix}")
    return ArtifactOrchestrationStateIndexEntry(
        storage_key=storage_key,
        status=status,
        state_directory=(root / storage_key).as_posix(),
        reason_code="state-integrity-failed",
        reason=f"{status.value.upper()}: test fixture",
    )


def make_snapshot(
    root: Path,
    entries=(),
) -> ArtifactOrchestrationStateIndexSnapshot:
    ordered = tuple(sorted(tuple(entries), key=lambda item: item.storage_key))
    valid_count = sum(
        item.status is ArtifactOrchestrationStateIndexEntryStatus.VALID
        for item in ordered
    )
    altered_count = sum(
        item.status is ArtifactOrchestrationStateIndexEntryStatus.ALTERED
        for item in ordered
    )
    unreadable_count = sum(
        item.status is ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
        for item in ordered
    )
    return ArtifactOrchestrationStateIndexSnapshot(
        index_id="orchestration-state-index:test",
        policy_id="policy:state-index-test",
        policy_hash=sha("index-policy"),
        state_root=root.as_posix(),
        requested_by="ELMAN_NEXUS",
        indexed_at=INDEXED_AT,
        entries=ordered,
        valid_count=valid_count,
        altered_count=altered_count,
        unreadable_count=unreadable_count,
    )


def make_policy(**changes):
    values = {
        "policy_id": "policy:state-selection-001",
        "strategy": (
            ArtifactOrchestrationStateSelectionStrategy.LATEST_PERSISTED
        ),
        "reject_ambiguous": True,
        "max_snapshot_entries": 100,
        "max_eligible_entries": 100,
    }
    values.update(changes)
    return ArtifactOrchestrationStateSelectionPolicy(**values)


def make_request(snapshot, **changes):
    values = {
        "request_id": "state-selection-request:001",
        "snapshot_json": snapshot.to_json(),
        "requested_by": "ELMAN_NEXUS",
        "requested_at": REQUESTED_AT,
        "expected_snapshot_hash": snapshot.snapshot_hash,
    }
    values.update(changes)
    return ArtifactOrchestrationStateSelectionRequest(**values)


def select(snapshot, *, policy=None, **request_changes):
    effective_policy = policy or make_policy()
    request = make_request(snapshot, **request_changes)
    return ArtifactOrchestrationStateSelector(
        effective_policy
    ).select(request)


class StateSelectionPolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(
            strategy=(
                ArtifactOrchestrationStateSelectionStrategy.OLDEST_PERSISTED
            ),
            reject_ambiguous=False,
        )
        self.assertEqual(
            ArtifactOrchestrationStateSelectionPolicy.from_json(
                policy.to_json()
            ),
            policy,
        )

    def test_policy_rejects_invalid_strategy(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            make_policy(strategy="random")

    def test_policy_rejects_non_boolean_ambiguity_flag(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            make_policy(reject_ambiguous="yes")

    def test_policy_rejects_zero_snapshot_limit(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            make_policy(max_snapshot_entries=0)

    def test_policy_rejects_zero_eligible_limit(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            make_policy(max_eligible_entries=0)

    def test_policy_rejects_eligible_limit_above_snapshot_limit(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            make_policy(
                max_snapshot_entries=5,
                max_eligible_entries=6,
            )

    def test_policy_rejects_unsupported_version(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionPolicy(
                policy_id="policy:state-selection-001",
                version=2,
            )

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:other"  # type: ignore[misc]


class StateSelectionRequestTests(unittest.TestCase):
    def test_request_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(
                snapshot,
                project_id="project:alpha",
                require_can_resume=True,
            )
            restored = ArtifactOrchestrationStateSelectionRequest.from_json(
                request.to_json()
            )
            self.assertEqual(restored, request)
            restored.verify_hash()

    def test_request_normalizes_status_order(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(
                snapshot,
                allowed_assessment_statuses=(
                    ResumeAssessmentStatus.TERMINAL,
                    ResumeAssessmentStatus.READY,
                ),
            )
            self.assertEqual(
                request.allowed_assessment_statuses,
                (
                    ResumeAssessmentStatus.READY,
                    ResumeAssessmentStatus.TERMINAL,
                ),
            )

    def test_request_rejects_duplicate_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                make_request(
                    snapshot,
                    allowed_assessment_statuses=(
                        ResumeAssessmentStatus.READY,
                        ResumeAssessmentStatus.READY,
                    ),
                )

    def test_request_allows_empty_status_set(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(
                snapshot,
                allowed_assessment_statuses=(),
            )
            self.assertEqual(request.allowed_assessment_statuses, ())

    def test_request_rejects_invalid_status(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                make_request(
                    snapshot,
                    allowed_assessment_statuses=("unknown",),
                )

    def test_request_rejects_non_boolean_resume_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                make_request(snapshot, require_can_resume="yes")

    def test_request_rejects_inverted_time_window(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                make_request(
                    snapshot,
                    persisted_not_before="2026-08-05T05:00:00Z",
                    persisted_not_after="2026-08-05T04:00:00Z",
                )

    def test_request_accepts_equal_time_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(
                snapshot,
                persisted_not_before="2026-08-05T04:00:00Z",
                persisted_not_after="2026-08-05T04:00:00Z",
            )
            self.assertEqual(
                request.persisted_not_before,
                request.persisted_not_after,
            )

    def test_request_rejects_non_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                make_request(
                    snapshot,
                    requested_at=datetime(
                        2026,
                        8,
                        5,
                        tzinfo=timezone(timedelta(hours=1)),
                    ),
                )

    def test_request_normalizes_utc_datetime(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(
                snapshot,
                requested_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            self.assertEqual(
                request.requested_at,
                "2026-08-05T00:00:00.000000Z",
            )

    def test_request_rejects_wrong_expected_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                make_request(
                    snapshot,
                    expected_snapshot_hash=sha("wrong"),
                )

    def test_request_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(snapshot)
            data = request.to_dict()
            data["project_id"] = "project:changed"
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                ArtifactOrchestrationStateSelectionRequest.from_dict(data)

    def test_request_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(snapshot)
            data = request.to_dict()
            del data["request_hash"]
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                ArtifactOrchestrationStateSelectionRequest.from_dict(data)

    def test_request_rejects_missing_snapshot_json(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            request = make_request(snapshot)
            data = request.to_dict()
            del data["snapshot_json"]
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                ArtifactOrchestrationStateSelectionRequest.from_dict(data)

    def test_request_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            request = make_request(make_snapshot(Path(directory)))
            with self.assertRaises(FrozenInstanceError):
                request.project_id = "project:other"  # type: ignore[misc]


class StateSelectionRecordTests(unittest.TestCase):
    def test_eligible_record_json_round_trip(self):
        record = ArtifactOrchestrationStateSelectionRecord(
            storage_key="a" * 64,
            entry_hash=sha("entry"),
            entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
            decision=(
                ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
            ),
            reason_codes=(),
            primary_rank="2026-08-05T04:00:00Z",
            rank_position=1,
        )
        self.assertEqual(
            ArtifactOrchestrationStateSelectionRecord.from_json(
                record.to_json()
            ),
            record,
        )

    def test_excluded_record_json_round_trip(self):
        record = ArtifactOrchestrationStateSelectionRecord(
            storage_key="a" * 64,
            entry_hash=sha("entry"),
            entry_status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            decision=(
                ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
            ),
            reason_codes=("entry-not-valid",),
        )
        self.assertEqual(
            ArtifactOrchestrationStateSelectionRecord.from_json(
                record.to_json()
            ),
            record,
        )

    def test_eligible_record_requires_valid_entry(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=(
                    ArtifactOrchestrationStateIndexEntryStatus.ALTERED
                ),
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                ),
                reason_codes=(),
                primary_rank="2026-08-05T04:00:00Z",
                rank_position=1,
            )

    def test_eligible_record_rejects_reasons(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                ),
                reason_codes=("project-id-mismatch",),
                primary_rank="2026-08-05T04:00:00Z",
                rank_position=1,
            )

    def test_eligible_record_requires_rank(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
                ),
                reason_codes=(),
            )

    def test_excluded_record_requires_reason(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
                ),
                reason_codes=(),
            )

    def test_excluded_record_rejects_rank(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
                ),
                reason_codes=("project-id-mismatch",),
                primary_rank="2026-08-05T04:00:00Z",
                rank_position=1,
            )

    def test_record_rejects_unsorted_reasons(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelectionRecord(
                storage_key="a" * 64,
                entry_hash=sha("entry"),
                entry_status=ArtifactOrchestrationStateIndexEntryStatus.VALID,
                decision=(
                    ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
                ),
                reason_codes=(
                    "project-id-mismatch",
                    "entry-not-valid",
                ),
            )

    def test_record_rejects_tampered_hash(self):
        record = ArtifactOrchestrationStateSelectionRecord(
            storage_key="a" * 64,
            entry_hash=sha("entry"),
            entry_status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            decision=(
                ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
            ),
            reason_codes=("entry-not-valid",),
        )
        data = record.to_dict()
        data["storage_key"] = "b" * 64
        with self.assertRaises(
            ArtifactOrchestrationStateSelectionIntegrityError
        ):
            ArtifactOrchestrationStateSelectionRecord.from_dict(data)

    def test_record_is_frozen(self):
        record = ArtifactOrchestrationStateSelectionRecord(
            storage_key="a" * 64,
            entry_hash=sha("entry"),
            entry_status=ArtifactOrchestrationStateIndexEntryStatus.ALTERED,
            decision=(
                ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED
            ),
            reason_codes=("entry-not-valid",),
        )
        with self.assertRaises(FrozenInstanceError):
            record.storage_key = "b" * 64  # type: ignore[misc]


class StateSelectorBehaviorTests(unittest.TestCase):
    def test_empty_snapshot_returns_no_match(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.NO_MATCH,
            )
            self.assertEqual(result.eligible_count, 0)
            self.assertIsNone(result.selected_entry)

    def test_single_valid_entry_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="001",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (entry,)))
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.SELECTED,
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                entry.storage_key,
            )

    def test_latest_strategy_selects_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = make_valid_entry(
                root,
                suffix="older",
                persisted_at="2026-08-05T03:00:00Z",
            )
            newer = make_valid_entry(
                root,
                suffix="newer",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (older, newer)))
            self.assertEqual(
                result.selected_entry.storage_key,
                newer.storage_key,
            )

    def test_oldest_strategy_selects_oldest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = make_valid_entry(
                root,
                suffix="older",
                persisted_at="2026-08-05T03:00:00Z",
            )
            newer = make_valid_entry(
                root,
                suffix="newer",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (newer, older)),
                policy=make_policy(
                    strategy=(
                        ArtifactOrchestrationStateSelectionStrategy.OLDEST_PERSISTED
                    )
                ),
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                older.storage_key,
            )

    def test_equal_primary_rank_is_ambiguous_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_valid_entry(
                root,
                suffix="first",
                persisted_at="2026-08-05T04:00:00Z",
            )
            second = make_valid_entry(
                root,
                suffix="second",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (first, second)))
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.AMBIGUOUS,
            )
            self.assertIsNone(result.selected_entry)

    def test_equal_primary_rank_can_use_storage_key_tiebreak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_valid_entry(
                root,
                suffix="first",
                persisted_at="2026-08-05T04:00:00Z",
            )
            second = make_valid_entry(
                root,
                suffix="second",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (first, second)),
                policy=make_policy(reject_ambiguous=False),
            )
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.SELECTED,
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                min(first.storage_key, second.storage_key),
            )

    def test_nonvalid_entries_are_never_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            altered = make_nonvalid_entry(root, suffix="altered")
            unreadable = make_nonvalid_entry(
                root,
                suffix="unreadable",
                status=(
                    ArtifactOrchestrationStateIndexEntryStatus.UNREADABLE
                ),
            )
            result = select(make_snapshot(root, (altered, unreadable)))
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.NO_MATCH,
            )
            self.assertEqual(result.excluded_count, 2)
            self.assertTrue(
                all(
                    "entry-not-valid" in record.reason_codes
                    for record in result.records
                )
            )

    def test_persistence_id_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_valid_entry(
                root,
                suffix="first",
                persisted_at="2026-08-05T05:00:00Z",
            )
            second = make_valid_entry(
                root,
                suffix="second",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (first, second)),
                persistence_id=second.persistence_id,
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                second.storage_key,
            )

    def test_unknown_persistence_id_returns_no_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (entry,)),
                persistence_id=(
                    "orchestration-persistence:" + sha("unknown")
                ),
            )
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.NO_MATCH,
            )

    def test_project_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha = make_valid_entry(
                root,
                suffix="alpha",
                project_id="project:alpha",
                persisted_at="2026-08-05T03:00:00Z",
            )
            beta = make_valid_entry(
                root,
                suffix="beta",
                project_id="project:beta",
                persisted_at="2026-08-05T05:00:00Z",
            )
            result = select(
                make_snapshot(root, (alpha, beta)),
                project_id="project:alpha",
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                alpha.storage_key,
            )

    def test_plan_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_valid_entry(
                root,
                suffix="first",
                plan_id="plan:target",
                persisted_at="2026-08-05T03:00:00Z",
            )
            second = make_valid_entry(
                root,
                suffix="second",
                plan_id="plan:other",
                persisted_at="2026-08-05T05:00:00Z",
            )
            result = select(
                make_snapshot(root, (first, second)),
                plan_id="plan:target",
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                first.storage_key,
            )

    def test_checkpoint_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_valid_entry(
                root,
                suffix="first",
                checkpoint_id="checkpoint:target",
                persisted_at="2026-08-05T03:00:00Z",
            )
            second = make_valid_entry(
                root,
                suffix="second",
                checkpoint_id="checkpoint:other",
                persisted_at="2026-08-05T05:00:00Z",
            )
            result = select(
                make_snapshot(root, (first, second)),
                checkpoint_id="checkpoint:target",
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                first.storage_key,
            )

    def test_assessment_status_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = make_valid_entry(
                root,
                suffix="ready",
                assessment_status=ResumeAssessmentStatus.READY,
                can_resume=True,
                persisted_at="2026-08-05T03:00:00Z",
            )
            terminal = make_valid_entry(
                root,
                suffix="terminal",
                assessment_status=ResumeAssessmentStatus.TERMINAL,
                can_resume=False,
                persisted_at="2026-08-05T05:00:00Z",
            )
            result = select(
                make_snapshot(root, (ready, terminal)),
                allowed_assessment_statuses=(
                    ResumeAssessmentStatus.READY,
                ),
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                ready.storage_key,
            )

    def test_require_resumable_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = make_valid_entry(
                root,
                suffix="ready",
                assessment_status=ResumeAssessmentStatus.READY,
                can_resume=True,
                persisted_at="2026-08-05T03:00:00Z",
            )
            terminal = make_valid_entry(
                root,
                suffix="terminal",
                assessment_status=ResumeAssessmentStatus.TERMINAL,
                can_resume=False,
                persisted_at="2026-08-05T05:00:00Z",
            )
            result = select(
                make_snapshot(root, (ready, terminal)),
                require_can_resume=True,
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                ready.storage_key,
            )

    def test_require_nonresumable_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = make_valid_entry(
                root,
                suffix="ready",
                can_resume=True,
                persisted_at="2026-08-05T05:00:00Z",
            )
            terminal = make_valid_entry(
                root,
                suffix="terminal",
                assessment_status=ResumeAssessmentStatus.TERMINAL,
                can_resume=False,
                persisted_at="2026-08-05T03:00:00Z",
            )
            result = select(
                make_snapshot(root, (ready, terminal)),
                require_can_resume=False,
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                terminal.storage_key,
            )

    def test_lower_time_bound_is_inclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (entry,)),
                persisted_not_before="2026-08-05T04:00:00Z",
            )
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.SELECTED,
            )

    def test_upper_time_bound_is_inclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(
                make_snapshot(root, (entry,)),
                persisted_not_after="2026-08-05T04:00:00Z",
            )
            self.assertIs(
                result.status,
                ArtifactOrchestrationStateSelectionStatus.SELECTED,
            )

    def test_time_window_excludes_outside_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = make_valid_entry(
                root,
                suffix="early",
                persisted_at="2026-08-05T02:00:00Z",
            )
            inside = make_valid_entry(
                root,
                suffix="inside",
                persisted_at="2026-08-05T04:00:00Z",
            )
            late = make_valid_entry(
                root,
                suffix="late",
                persisted_at="2026-08-05T06:00:00Z",
            )
            result = select(
                make_snapshot(root, (early, inside, late)),
                persisted_not_before="2026-08-05T03:00:00Z",
                persisted_not_after="2026-08-05T05:00:00Z",
            )
            self.assertEqual(
                result.selected_entry.storage_key,
                inside.storage_key,
            )

    def test_multiple_filter_failures_are_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                project_id="project:alpha",
                assessment_status=ResumeAssessmentStatus.TERMINAL,
                can_resume=False,
                persisted_at="2026-08-05T02:00:00Z",
            )
            result = select(
                make_snapshot(root, (entry,)),
                project_id="project:beta",
                require_can_resume=True,
                allowed_assessment_statuses=(
                    ResumeAssessmentStatus.READY,
                ),
                persisted_not_before="2026-08-05T03:00:00Z",
            )
            self.assertEqual(
                result.records[0].reason_codes,
                tuple(sorted(result.records[0].reason_codes)),
            )
            self.assertGreaterEqual(
                len(result.records[0].reason_codes),
                4,
            )

    def test_records_put_eligible_before_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = make_valid_entry(
                root,
                suffix="valid",
                persisted_at="2026-08-05T04:00:00Z",
            )
            altered = make_nonvalid_entry(root, suffix="altered")
            result = select(make_snapshot(root, (altered, valid)))
            self.assertIs(
                result.records[0].decision,
                ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE,
            )
            self.assertIs(
                result.records[1].decision,
                ArtifactOrchestrationStateSelectionRecordDecision.EXCLUDED,
            )

    def test_rank_positions_are_contiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = (
                make_valid_entry(
                    root,
                    suffix="one",
                    persisted_at="2026-08-05T03:00:00Z",
                ),
                make_valid_entry(
                    root,
                    suffix="two",
                    persisted_at="2026-08-05T04:00:00Z",
                ),
                make_valid_entry(
                    root,
                    suffix="three",
                    persisted_at="2026-08-05T05:00:00Z",
                ),
            )
            result = select(make_snapshot(root, entries))
            positions = [
                record.rank_position
                for record in result.records
                if record.decision
                is ArtifactOrchestrationStateSelectionRecordDecision.ELIGIBLE
            ]
            self.assertEqual(positions, [1, 2, 3])

    def test_same_inputs_produce_same_result_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            snapshot = make_snapshot(root, (entry,))
            first = select(snapshot)
            second = select(snapshot)
            self.assertEqual(first.result_hash, second.result_hash)

    def test_snapshot_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = (
                make_valid_entry(
                    root,
                    suffix="one",
                    persisted_at="2026-08-05T03:00:00Z",
                ),
                make_valid_entry(
                    root,
                    suffix="two",
                    persisted_at="2026-08-05T04:00:00Z",
                ),
            )
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionLimitError
            ):
                select(
                    make_snapshot(root, entries),
                    policy=make_policy(
                        max_snapshot_entries=1,
                        max_eligible_entries=1,
                    ),
                )

    def test_eligible_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = (
                make_valid_entry(
                    root,
                    suffix="one",
                    persisted_at="2026-08-05T03:00:00Z",
                ),
                make_valid_entry(
                    root,
                    suffix="two",
                    persisted_at="2026-08-05T04:00:00Z",
                ),
            )
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionLimitError
            ):
                select(
                    make_snapshot(root, entries),
                    policy=make_policy(
                        max_snapshot_entries=2,
                        max_eligible_entries=1,
                    ),
                )

    def test_selector_rejects_wrong_request_type(self):
        with self.assertRaises(ArtifactOrchestrationStateSelectionError):
            ArtifactOrchestrationStateSelector(
                make_policy()
            ).select("request")  # type: ignore[arg-type]

    def test_selector_is_frozen(self):
        selector = ArtifactOrchestrationStateSelector(make_policy())
        with self.assertRaises(FrozenInstanceError):
            selector.policy = make_policy()  # type: ignore[misc]


class StateSelectionResultTests(unittest.TestCase):
    def test_selected_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (entry,)))
            restored = ArtifactOrchestrationStateSelectionResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)
            restored.verify_hash()

    def test_no_match_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            restored = ArtifactOrchestrationStateSelectionResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)

    def test_ambiguous_result_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = (
                make_valid_entry(
                    root,
                    suffix="one",
                    persisted_at="2026-08-05T04:00:00Z",
                ),
                make_valid_entry(
                    root,
                    suffix="two",
                    persisted_at="2026-08-05T04:00:00Z",
                ),
            )
            result = select(make_snapshot(root, entries))
            restored = ArtifactOrchestrationStateSelectionResult.from_json(
                result.to_json()
            )
            self.assertEqual(restored, result)

    def test_result_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            data = result.to_dict()
            data["reason"] = "changed"
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                ArtifactOrchestrationStateSelectionResult.from_dict(data)

    def test_result_rejects_missing_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            data = result.to_dict()
            del data["result_hash"]
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                ArtifactOrchestrationStateSelectionResult.from_dict(data)

    def test_result_rejects_counter_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                replace(
                    result,
                    eligible_count=1,
                    result_hash=None,
                )

    def test_no_match_rejects_selected_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            selected = select(make_snapshot(root, (entry,)))
            empty = select(make_snapshot(root))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                replace(
                    empty,
                    selected_entry_json=selected.selected_entry_json,
                    selected_record_hash=(
                        selected.selected_record_hash
                    ),
                    result_hash=None,
                )

    def test_selected_result_rejects_wrong_record_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (entry,)))
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionIntegrityError
            ):
                replace(
                    result,
                    selected_record_hash=sha("wrong"),
                    result_hash=None,
                )

    def test_result_rejects_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_valid_entry(
                root,
                suffix="one",
                persisted_at="2026-08-05T04:00:00Z",
            )
            result = select(make_snapshot(root, (entry,)))
            record = result.records[0]
            with self.assertRaises(
                ArtifactOrchestrationStateSelectionError
            ):
                replace(
                    result,
                    records=(record, record),
                    eligible_count=2,
                    result_hash=None,
                )

    def test_result_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            result = select(make_snapshot(Path(directory)))
            with self.assertRaises(FrozenInstanceError):
                result.reason = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
