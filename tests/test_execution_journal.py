from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.execution_journal import (
    GENESIS_HASH,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionJournal,
    ExecutionJournalError,
    JournalIntegrityError,
    JournalSequenceError,
    JournalTimestampError,
)


T0 = "2026-08-04T00:00:00Z"
T1 = "2026-08-04T00:00:01Z"
T2 = "2026-08-04T00:00:02Z"
T3 = "2026-08-04T00:00:03Z"


def journal_with_created() -> ExecutionJournal:
    journal = ExecutionJournal("plan:001")
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
        payload={"objective": "Build safely"},
    )
    return journal


class ExecutionEventTests(unittest.TestCase):
    def test_event_computes_hash(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            agent_id="ELMAN_NEXUS",
            previous_hash=GENESIS_HASH,
        )

        self.assertEqual(len(event.event_hash or ""), 64)
        self.assertEqual(event.event_hash, event.compute_hash())

    def test_event_hash_is_deterministic_for_payload_order(self) -> None:
        left = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
            payload={"b": 2, "a": 1},
        )
        right = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
            payload={"a": 1, "b": 2},
        )

        self.assertEqual(left.event_hash, right.event_hash)
        self.assertEqual(left.to_json(), right.to_json())

    def test_event_normalizes_timestamp_to_microseconds(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
        )

        self.assertEqual(
            event.timestamp,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_event_accepts_utc_datetime(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=datetime(2026, 8, 4, tzinfo=UTC),
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
        )

        self.assertEqual(
            event.timestamp,
            "2026-08-04T00:00:00.000000Z",
        )

    def test_event_rejects_naive_datetime(self) -> None:
        with self.assertRaises(JournalTimestampError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp=datetime(2026, 8, 4),
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
            )

    def test_event_rejects_non_utc_datetime(self) -> None:
        tz = timezone(timedelta(hours=1))
        with self.assertRaises(JournalTimestampError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp=datetime(2026, 8, 4, tzinfo=tz),
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
            )

    def test_event_rejects_timestamp_without_z(self) -> None:
        with self.assertRaises(JournalTimestampError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp="2026-08-04T00:00:00+00:00",
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
            )

    def test_step_event_requires_step_id(self) -> None:
        with self.assertRaises(ExecutionJournalError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.STEP_READY,
                timestamp=T0,
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
            )

    def test_plan_event_rejects_step_id(self) -> None:
        with self.assertRaises(ExecutionJournalError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp=T0,
                plan_id="plan:001",
                step_id="step.one",
                previous_hash=GENESIS_HASH,
            )

    def test_started_step_requires_agent_id(self) -> None:
        with self.assertRaises(ExecutionJournalError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.STEP_STARTED,
                timestamp=T0,
                plan_id="plan:001",
                step_id="step.one",
                previous_hash=GENESIS_HASH,
            )

    def test_ready_step_can_omit_agent_id(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.STEP_READY,
            timestamp=T0,
            plan_id="plan:001",
            step_id="step.one",
            previous_hash=GENESIS_HASH,
        )

        self.assertIsNone(event.agent_id)

    def test_event_rejects_non_positive_sequence(self) -> None:
        with self.assertRaises(JournalSequenceError):
            ExecutionEvent(
                sequence=0,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp=T0,
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
            )

    def test_event_rejects_non_json_payload(self) -> None:
        with self.assertRaises(ExecutionJournalError):
            ExecutionEvent(
                sequence=1,
                event_type=ExecutionEventType.PLAN_CREATED,
                timestamp=T0,
                plan_id="plan:001",
                previous_hash=GENESIS_HASH,
                payload={"bad": object()},
            )

    def test_event_payload_is_immutable(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
            payload={"nested": {"value": 1}},
        )

        with self.assertRaises(TypeError):
            event.payload["new"] = "value"  # type: ignore[index]

        nested = event.payload["nested"]
        with self.assertRaises(TypeError):
            nested["value"] = 2  # type: ignore[index]

    def test_event_round_trip(self) -> None:
        original = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            agent_id="ELMAN_NEXUS",
            previous_hash=GENESIS_HASH,
            payload={"safe": True},
        )

        restored = ExecutionEvent.from_json(original.to_json())

        self.assertEqual(restored, original)
        restored.verify_hash()

    def test_event_detects_tampered_payload(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
            payload={"value": 1},
        )
        data = event.to_dict()
        data["payload"]["value"] = 2

        with self.assertRaises(JournalIntegrityError):
            ExecutionEvent.from_dict(data)

    def test_event_requires_serialized_hash(self) -> None:
        event = ExecutionEvent(
            sequence=1,
            event_type=ExecutionEventType.PLAN_CREATED,
            timestamp=T0,
            plan_id="plan:001",
            previous_hash=GENESIS_HASH,
        )
        data = event.to_dict()
        del data["event_hash"]

        with self.assertRaises(JournalIntegrityError):
            ExecutionEvent.from_dict(data)


class ExecutionJournalAppendTests(unittest.TestCase):
    def test_first_event_must_be_plan_created(self) -> None:
        journal = ExecutionJournal("plan:001")

        with self.assertRaises(ExecutionJournalError):
            journal.append(
                ExecutionEventType.PLAN_STARTED,
                T0,
            )

    def test_append_assigns_monotonic_sequences(self) -> None:
        journal = journal_with_created()
        second = journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T1,
            payload={"approval_reference": "approval:001"},
        )
        third = journal.append(
            ExecutionEventType.PLAN_STARTED,
            T2,
            agent_id="ELMAN_NEXUS",
        )

        self.assertEqual(second.sequence, 2)
        self.assertEqual(third.sequence, 3)

    def test_append_links_hash_chain(self) -> None:
        journal = journal_with_created()
        second = journal.append(
            ExecutionEventType.STEP_READY,
            T1,
            step_id="step.one",
        )

        self.assertEqual(
            second.previous_hash,
            journal.events[0].event_hash,
        )
        self.assertEqual(journal.head_hash, second.event_hash)

    def test_append_rejects_plan_id_mismatch(self) -> None:
        journal = journal_with_created()
        foreign = ExecutionEvent(
            sequence=2,
            event_type=ExecutionEventType.PLAN_APPROVED,
            timestamp=T1,
            plan_id="plan:other",
            previous_hash=journal.head_hash,
        )

        with self.assertRaises(ExecutionJournalError):
            journal.append_event(foreign)

    def test_append_rejects_sequence_gap(self) -> None:
        journal = journal_with_created()
        event = ExecutionEvent(
            sequence=3,
            event_type=ExecutionEventType.PLAN_APPROVED,
            timestamp=T1,
            plan_id=journal.plan_id,
            previous_hash=journal.head_hash,
        )

        with self.assertRaises(JournalSequenceError):
            journal.append_event(event)

    def test_append_rejects_wrong_previous_hash(self) -> None:
        journal = journal_with_created()
        event = ExecutionEvent(
            sequence=2,
            event_type=ExecutionEventType.PLAN_APPROVED,
            timestamp=T1,
            plan_id=journal.plan_id,
            previous_hash=GENESIS_HASH,
        )

        with self.assertRaises(JournalIntegrityError):
            journal.append_event(event)

    def test_append_rejects_timestamp_regression(self) -> None:
        journal = journal_with_created()

        with self.assertRaises(JournalTimestampError):
            journal.append(
                ExecutionEventType.PLAN_APPROVED,
                "2026-08-03T23:59:59Z",
            )

    def test_equal_timestamps_are_allowed(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T0,
        )

        self.assertEqual(journal.event_count, 2)

    def test_duplicate_plan_created_is_rejected(self) -> None:
        journal = journal_with_created()

        with self.assertRaises(ExecutionJournalError):
            journal.append(
                ExecutionEventType.PLAN_CREATED,
                T1,
            )

    def test_append_after_completed_plan_is_rejected(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            T1,
        )

        with self.assertRaises(ExecutionJournalError):
            journal.append(
                ExecutionEventType.PLAN_BLOCKED,
                T2,
            )

    def test_append_after_failed_plan_is_rejected(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.PLAN_FAILED,
            T1,
        )

        with self.assertRaises(ExecutionJournalError):
            journal.append(
                ExecutionEventType.PLAN_STARTED,
                T2,
            )

    def test_events_property_is_tuple(self) -> None:
        journal = journal_with_created()

        self.assertIsInstance(journal.events, tuple)

    def test_events_for_step_filters_deterministically(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.STEP_READY,
            T1,
            step_id="step.one",
        )
        journal.append(
            ExecutionEventType.STEP_READY,
            T2,
            step_id="step.two",
        )
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            T3,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )

        self.assertEqual(
            tuple(
                event.event_type
                for event in journal.events_for_step("step.one")
            ),
            (
                ExecutionEventType.STEP_READY,
                ExecutionEventType.STEP_ASSIGNED,
            ),
        )


class ExecutionJournalSealTests(unittest.TestCase):
    def test_seal_contains_count_head_and_digest(self) -> None:
        journal = journal_with_created()
        seal = journal.seal()

        self.assertEqual(seal.event_count, 1)
        self.assertEqual(seal.head_hash, journal.head_hash)
        self.assertEqual(len(seal.journal_hash), 64)

    def test_seal_is_deterministic(self) -> None:
        left = journal_with_created()
        right = journal_with_created()

        self.assertEqual(
            left.seal().journal_hash,
            right.seal().journal_hash,
        )

    def test_empty_journal_can_be_sealed(self) -> None:
        journal = ExecutionJournal("plan:001")
        seal = journal.seal()

        self.assertEqual(seal.event_count, 0)
        self.assertEqual(seal.head_hash, GENESIS_HASH)

    def test_jsonl_has_final_seal(self) -> None:
        journal = journal_with_created()
        records = [
            json.loads(line)
            for line in journal.to_jsonl().splitlines()
        ]

        self.assertEqual(records[0]["record_type"], "event")
        self.assertEqual(records[-1]["record_type"], "seal")

    def test_jsonl_round_trip(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.STEP_READY,
            T1,
            step_id="step.one",
        )
        payload = journal.to_jsonl()

        restored = ExecutionJournal.from_jsonl(payload)

        self.assertEqual(restored.events, journal.events)
        self.assertEqual(restored.to_jsonl(), payload)

    def test_jsonl_detects_modified_event(self) -> None:
        journal = journal_with_created()
        records = journal.to_jsonl().splitlines()
        event = json.loads(records[0])
        event["payload"]["objective"] = "Tampered"
        records[0] = json.dumps(event)

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl("\n".join(records) + "\n")

    def test_jsonl_detects_deleted_middle_event(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.STEP_READY,
            T1,
            step_id="step.one",
        )
        journal.append(
            ExecutionEventType.STEP_ASSIGNED,
            T2,
            step_id="step.one",
            agent_id="ELMAN_CORE",
        )
        records = journal.to_jsonl().splitlines()
        del records[1]

        with self.assertRaises(
            (JournalSequenceError, JournalIntegrityError)
        ):
            ExecutionJournal.from_jsonl("\n".join(records) + "\n")

    def test_jsonl_detects_deleted_tail_event(self) -> None:
        journal = journal_with_created()
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T1,
        )
        records = journal.to_jsonl().splitlines()
        del records[-2]

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl("\n".join(records) + "\n")

    def test_jsonl_detects_deleted_seal(self) -> None:
        journal = journal_with_created()
        records = journal.to_jsonl().splitlines()
        payload = records[0] + "\n"

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(payload)

    def test_jsonl_rejects_blank_record(self) -> None:
        journal = journal_with_created()
        payload = journal.to_jsonl().replace("\n", "\n\n", 1)

        with self.assertRaises(ExecutionJournalError):
            ExecutionJournal.from_jsonl(payload)

    def test_jsonl_rejects_record_after_seal(self) -> None:
        journal = journal_with_created()
        payload = journal.to_jsonl() + journal.events[0].to_json() + "\n"

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(payload)

    def test_expected_plan_id_is_enforced(self) -> None:
        payload = journal_with_created().to_jsonl()

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(
                payload,
                expected_plan_id="plan:other",
            )

    def test_expected_event_count_is_enforced(self) -> None:
        payload = journal_with_created().to_jsonl()

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(
                payload,
                expected_event_count=2,
            )

    def test_expected_head_hash_is_enforced(self) -> None:
        payload = journal_with_created().to_jsonl()

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(
                payload,
                expected_head_hash="1" * 64,
            )

    def test_expected_journal_hash_is_enforced(self) -> None:
        payload = journal_with_created().to_jsonl()

        with self.assertRaises(JournalIntegrityError):
            ExecutionJournal.from_jsonl(
                payload,
                expected_journal_hash="1" * 64,
            )

    def test_replay_validates_and_returns_tuple(self) -> None:
        journal = journal_with_created()
        replayed = journal.replay()

        self.assertEqual(replayed, journal.events)
        self.assertIsInstance(replayed, tuple)

    def test_from_events_rebuilds_chain(self) -> None:
        original = journal_with_created()
        original.append(
            ExecutionEventType.PLAN_APPROVED,
            T1,
        )

        restored = ExecutionJournal.from_events(
            original.plan_id,
            original.events,
        )

        self.assertEqual(restored.to_jsonl(), original.to_jsonl())


if __name__ == "__main__":
    unittest.main()
