from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.execution_journal import ExecutionEventType, ExecutionJournal
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
)
from elman_os.metacognitive_loop_detection import (
    MetacognitiveLoopDetectionError,
    MetacognitiveLoopDetectionIntegrityError,
    MetacognitiveLoopDetectionPolicy,
    MetacognitiveLoopDetectionPolicyError,
    MetacognitiveLoopDetectionRecord,
    MetacognitiveLoopDetectionRequest,
    MetacognitiveLoopDetectionResult,
    MetacognitiveLoopDetectionStatus,
    MetacognitiveLoopDetector,
    MetacognitiveLoopPattern,
)


PLAN_ID = "plan:loop-detection-001"
PROJECT_ID = "project:elman-001"
OBSERVED_AT = "2026-08-06T04:00:00Z"
REQUESTED_AT = "2026-08-06T04:01:00Z"
COMPLETED_AT = "2026-08-06T04:02:00Z"
EVIDENCE = "evidence:journal-loop-001"


def digest(character: str) -> str:
    return character * 64


def timestamp(index: int) -> str:
    return f"2026-08-06T04:{index:02d}:00Z"


def make_policy(**changes):
    values = {
        "policy_id": "policy:metacognitive-loop-detection-v1",
        "minimum_repetitions": 3,
        "maximum_cycle_length": 8,
        "high_risk_repetitions": 4,
        "critical_risk_repetitions": 6,
        "base_confidence_bp": 7000,
        "repetition_confidence_increment_bp": 500,
        "include_plan_events": False,
        "fail_closed": True,
    }
    values.update(changes)
    return MetacognitiveLoopDetectionPolicy(**values)


def new_journal() -> ExecutionJournal:
    journal = ExecutionJournal(PLAN_ID)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        timestamp(0),
        payload={"project_id": PROJECT_ID},
    )
    return journal


def add_ready_repetitions(
    journal: ExecutionJournal,
    repetitions: int,
    *,
    step_id: str = "step.1",
    start_minute: int = 1,
) -> None:
    for index in range(repetitions):
        journal.append(
            ExecutionEventType.STEP_READY,
            timestamp(start_minute + index),
            step_id=step_id,
        )


def add_two_event_cycle(
    journal: ExecutionJournal,
    repetitions: int,
    *,
    step_id: str = "step.1",
    start_minute: int = 1,
) -> None:
    minute = start_minute
    for _ in range(repetitions):
        journal.append(
            ExecutionEventType.STEP_READY,
            timestamp(minute),
            step_id=step_id,
        )
        minute += 1
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            timestamp(minute),
            step_id=step_id,
            agent_id="ELMAN_AGENT",
            payload={"reason": "waiting"},
        )
        minute += 1


def make_context(journal: ExecutionJournal, **changes):
    seal = journal.seal()
    values = {
        "plan_id": journal.plan_id,
        "project_id": PROJECT_ID,
        "plan_state_hash": digest("a"),
        "journal_hash": seal.journal_hash,
        "checkpoint_hash": digest("c"),
        "evidence_references": (EVIDENCE, "evidence:checkpoint-001"),
        "observed_by": "ELMAN_SUPERVISOR",
        "observed_at": OBSERVED_AT,
        "objective": "Detect deterministic orchestration loops.",
    }
    values.update(changes)
    return MetacognitiveSupervisionContext.capture(**values)


def make_request(
    journal: ExecutionJournal,
    *,
    policy=None,
    context=None,
    requested_at=REQUESTED_AT,
    evidence=EVIDENCE,
):
    effective_policy = policy or make_policy()
    effective_context = context or make_context(journal)
    return MetacognitiveLoopDetectionRequest.capture(
        policy=effective_policy,
        context=effective_context,
        journal=journal,
        journal_evidence_reference=evidence,
        requested_by="ELMAN_SUPERVISOR",
        requested_at=requested_at,
        reason="Inspect the journal for repeated contiguous event cycles.",
    )


def detect(journal, *, policy=None, context=None, completed_at=COMPLETED_AT):
    request = make_request(journal, policy=policy, context=context)
    return MetacognitiveLoopDetector().detect(
        request=request,
        journal=journal,
        completed_at=completed_at,
    )


class PolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_round_trip(self):
        policy = make_policy(maximum_cycle_length=12)
        self.assertEqual(
            MetacognitiveLoopDetectionPolicy.from_json(policy.to_json()),
            policy,
        )

    def test_policy_rejects_disabled_fail_closed(self):
        with self.assertRaises(MetacognitiveLoopDetectionPolicyError):
            make_policy(fail_closed=False)

    def test_policy_rejects_minimum_below_two(self):
        with self.assertRaises(MetacognitiveLoopDetectionError):
            make_policy(minimum_repetitions=1)

    def test_policy_rejects_high_below_minimum(self):
        with self.assertRaises(MetacognitiveLoopDetectionPolicyError):
            make_policy(minimum_repetitions=5, high_risk_repetitions=4)

    def test_policy_rejects_critical_below_high(self):
        with self.assertRaises(MetacognitiveLoopDetectionPolicyError):
            make_policy(high_risk_repetitions=7, critical_risk_repetitions=6)

    def test_policy_rejects_invalid_confidence(self):
        with self.assertRaises(MetacognitiveLoopDetectionError):
            make_policy(base_confidence_bp=10001)

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.fail_closed = False  # type: ignore[misc]


class RequestTests(unittest.TestCase):
    def test_request_identifier_is_deterministic(self):
        journal = new_journal()
        self.assertEqual(
            make_request(journal).request_id,
            make_request(journal).request_id,
        )

    def test_request_round_trip(self):
        request = make_request(new_journal())
        restored = MetacognitiveLoopDetectionRequest.from_json(
            request.to_json()
        )
        self.assertEqual(restored, request)
        restored.verify_hash()

    def test_request_rejects_unbound_evidence(self):
        journal = new_journal()
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            make_request(journal, evidence="evidence:unknown")

    def test_request_rejects_context_for_other_journal(self):
        first = new_journal()
        second = new_journal()
        add_ready_repetitions(second, 1)
        context = make_context(first)
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            make_request(second, context=context)

    def test_request_rejects_time_before_observation(self):
        with self.assertRaises(MetacognitiveLoopDetectionPolicyError):
            make_request(
                new_journal(),
                requested_at="2026-08-06T03:59:59Z",
            )

    def test_request_accepts_utc_datetime(self):
        request = make_request(
            new_journal(),
            requested_at=datetime(2026, 8, 6, 4, 1, tzinfo=UTC),
        )
        self.assertEqual(request.requested_at, "2026-08-06T04:01:00.000000Z")

    def test_request_rejects_non_utc_datetime(self):
        with self.assertRaises(MetacognitiveLoopDetectionError):
            make_request(
                new_journal(),
                requested_at=datetime(
                    2026,
                    8,
                    6,
                    5,
                    1,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            )

    def test_request_rejects_tampered_hash(self):
        data = make_request(new_journal()).to_dict()
        data["reason"] = "tampered"
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            MetacognitiveLoopDetectionRequest.from_dict(data)


class DetectorTests(unittest.TestCase):
    def test_clear_result_for_non_repeating_journal(self):
        journal = new_journal()
        for index, step_id in enumerate(("step.1", "step.2", "step.3"), 1):
            journal.append(
                ExecutionEventType.STEP_READY,
                timestamp(index),
                step_id=step_id,
            )
        result = detect(journal)
        self.assertIs(result.status, MetacognitiveLoopDetectionStatus.CLEAR)
        self.assertEqual(result.records, ())
        self.assertEqual(result.inspected_event_count, 3)

    def test_detects_single_event_cycle(self):
        journal = new_journal()
        add_ready_repetitions(journal, 3)
        result = detect(journal)
        self.assertIs(
            result.status,
            MetacognitiveLoopDetectionStatus.LOOPS_DETECTED,
        )
        self.assertEqual(len(result.records), 1)
        pattern = result.patterns[0]
        self.assertEqual(pattern.cycle_length, 1)
        self.assertEqual(pattern.repetitions, 3)
        self.assertEqual(pattern.start_sequence, 2)
        self.assertEqual(pattern.end_sequence, 4)

    def test_detects_two_event_cycle(self):
        journal = new_journal()
        add_two_event_cycle(journal, 3)
        pattern = detect(journal).patterns[0]
        self.assertEqual(pattern.cycle_length, 2)
        self.assertEqual(pattern.repetitions, 3)
        self.assertEqual(
            pattern.event_signature,
            (
                "step.ready|step.1|-",
                "step.blocked|step.1|ELMAN_AGENT",
            ),
        )

    def test_selects_fundamental_cycle(self):
        journal = new_journal()
        add_ready_repetitions(journal, 6)
        pattern = detect(journal).patterns[0]
        self.assertEqual(pattern.cycle_length, 1)
        self.assertEqual(pattern.repetitions, 6)

    def test_detects_multiple_non_overlapping_loops(self):
        journal = new_journal()
        add_ready_repetitions(journal, 3, step_id="step.1")
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            timestamp(4),
            payload={"reason": "barrier"},
        )
        add_ready_repetitions(
            journal,
            3,
            step_id="step.2",
            start_minute=5,
        )
        result = detect(journal)
        self.assertEqual(len(result.patterns), 2)
        self.assertEqual(
            tuple(pattern.affected_step_ids for pattern in result.patterns),
            (("step.1",), ("step.2",)),
        )

    def test_plan_event_breaks_step_segments(self):
        journal = new_journal()
        add_ready_repetitions(journal, 2, step_id="step.1")
        journal.append(
            ExecutionEventType.PLAN_BLOCKED,
            timestamp(3),
            payload={"reason": "barrier"},
        )
        add_ready_repetitions(
            journal,
            1,
            step_id="step.1",
            start_minute=4,
        )
        self.assertIs(
            detect(journal).status,
            MetacognitiveLoopDetectionStatus.CLEAR,
        )

    def test_plan_events_can_be_included(self):
        journal = new_journal()
        for minute in range(1, 4):
            journal.append(
                ExecutionEventType.PLAN_BLOCKED,
                timestamp(minute),
                payload={"reason": "same"},
            )
        policy = make_policy(include_plan_events=True)
        pattern = detect(journal, policy=policy).patterns[0]
        self.assertEqual(pattern.event_signature, ("plan.blocked|-|-",))
        self.assertEqual(pattern.affected_step_ids, ())

    def test_medium_risk_at_minimum_repetitions(self):
        journal = new_journal()
        add_ready_repetitions(journal, 3)
        self.assertIs(
            detect(journal).patterns[0].risk_level,
            MetacognitiveRiskLevel.MEDIUM,
        )

    def test_high_risk_at_high_threshold(self):
        journal = new_journal()
        add_ready_repetitions(journal, 4)
        self.assertIs(
            detect(journal).patterns[0].risk_level,
            MetacognitiveRiskLevel.HIGH,
        )

    def test_critical_risk_at_critical_threshold(self):
        journal = new_journal()
        add_ready_repetitions(journal, 6)
        self.assertIs(
            detect(journal).patterns[0].risk_level,
            MetacognitiveRiskLevel.CRITICAL,
        )

    def test_confidence_increases_and_caps(self):
        journal = new_journal()
        add_ready_repetitions(journal, 10)
        policy = make_policy(
            base_confidence_bp=9000,
            repetition_confidence_increment_bp=1000,
        )
        self.assertEqual(detect(journal, policy=policy).patterns[0].confidence_bp, 10000)

    def test_finding_is_bound_to_pattern(self):
        journal = new_journal()
        add_ready_repetitions(journal, 3)
        record = detect(journal).records[0]
        self.assertIs(record.finding.kind, MetacognitiveFindingKind.LOOP)
        self.assertEqual(
            record.finding.affected_step_ids,
            record.pattern.affected_step_ids,
        )
        self.assertEqual(
            record.finding.evidence_references,
            record.pattern.evidence_references,
        )

    def test_different_agents_change_signature(self):
        journal = new_journal()
        for minute, agent in enumerate(
            ("ELMAN_AGENT_A", "ELMAN_AGENT_B", "ELMAN_AGENT_C"),
            1,
        ):
            journal.append(
                ExecutionEventType.STEP_BLOCKED,
                timestamp(minute),
                step_id="step.1",
                agent_id=agent,
                payload={"reason": "waiting"},
            )
        self.assertIs(
            detect(journal).status,
            MetacognitiveLoopDetectionStatus.CLEAR,
        )

    def test_maximum_cycle_length_is_enforced(self):
        journal = new_journal()
        cycle = ("step.1", "step.2", "step.3")
        minute = 1
        for _ in range(3):
            for step_id in cycle:
                journal.append(
                    ExecutionEventType.STEP_READY,
                    timestamp(minute),
                    step_id=step_id,
                )
                minute += 1
        self.assertIs(
            detect(
                journal,
                policy=make_policy(maximum_cycle_length=2),
            ).status,
            MetacognitiveLoopDetectionStatus.CLEAR,
        )
        self.assertEqual(
            detect(
                journal,
                policy=make_policy(maximum_cycle_length=3),
            ).patterns[0].cycle_length,
            3,
        )

    def test_detector_rejects_different_journal(self):
        first = new_journal()
        add_ready_repetitions(first, 3)
        request = make_request(first)
        second = new_journal()
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            MetacognitiveLoopDetector().detect(
                request=request,
                journal=second,
                completed_at=COMPLETED_AT,
            )

    def test_detector_rejects_completion_before_request(self):
        with self.assertRaises(MetacognitiveLoopDetectionPolicyError):
            MetacognitiveLoopDetector().detect(
                request=make_request(new_journal()),
                journal=new_journal(),
                completed_at="2026-08-06T04:00:59Z",
            )

    def test_detector_does_not_mutate_journal_or_context(self):
        journal = new_journal()
        add_ready_repetitions(journal, 3)
        context = make_context(journal)
        before = (journal.to_jsonl(), context.to_json())
        detect(journal, context=context)
        after = (journal.to_jsonl(), context.to_json())
        self.assertEqual(after, before)


class PatternRecordResultTests(unittest.TestCase):
    def make_result(self):
        journal = new_journal()
        add_two_event_cycle(journal, 3)
        return detect(journal)

    def test_pattern_round_trip(self):
        pattern = self.make_result().patterns[0]
        restored = MetacognitiveLoopPattern.from_json(pattern.to_json())
        self.assertEqual(restored, pattern)
        restored.verify_hash()

    def test_pattern_rejects_tampering(self):
        data = self.make_result().patterns[0].to_dict()
        data["repetitions"] = 4
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            MetacognitiveLoopPattern.from_dict(data)

    def test_pattern_span_must_match_cycle(self):
        pattern = self.make_result().patterns[0]
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            replace(pattern, end_sequence=pattern.end_sequence + 1)

    def test_record_round_trip(self):
        record = self.make_result().records[0]
        restored = MetacognitiveLoopDetectionRecord.from_json(
            record.to_json()
        )
        self.assertEqual(restored, record)
        restored.verify_hash()

    def test_record_rejects_tampered_finding(self):
        record = self.make_result().records[0]
        data = record.to_dict()
        finding = json.loads(data["finding_json"])
        finding["summary"] = "tampered"
        data["finding_json"] = json.dumps(finding)
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            MetacognitiveLoopDetectionRecord.from_dict(data)

    def test_result_round_trip(self):
        result = self.make_result()
        restored = MetacognitiveLoopDetectionResult.from_json(
            result.to_json()
        )
        self.assertEqual(restored, result)
        restored.verify_hash()

    def test_result_rejects_tampered_reason(self):
        data = self.make_result().to_dict()
        data["reason"] = "tampered"
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            MetacognitiveLoopDetectionResult.from_dict(data)

    def test_clear_result_rejects_records(self):
        result = self.make_result()
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            replace(result, status=MetacognitiveLoopDetectionStatus.CLEAR)

    def test_detected_result_requires_records(self):
        journal = new_journal()
        clear = detect(journal)
        with self.assertRaises(MetacognitiveLoopDetectionIntegrityError):
            replace(
                clear,
                status=MetacognitiveLoopDetectionStatus.LOOPS_DETECTED,
            )

    def test_contracts_are_frozen(self):
        pattern = self.make_result().patterns[0]
        with self.assertRaises(FrozenInstanceError):
            pattern.repetitions = 9  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
