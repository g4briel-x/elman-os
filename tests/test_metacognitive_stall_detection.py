from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from elman_os.execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
)
from elman_os.metacognitive_stall_detection import (
    METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION,
    MetacognitiveStallDetectionError,
    MetacognitiveStallDetectionIntegrityError,
    MetacognitiveStallDetectionPolicy,
    MetacognitiveStallDetectionPolicyError,
    MetacognitiveStallDetectionRecord,
    MetacognitiveStallDetectionRequest,
    MetacognitiveStallDetectionResult,
    MetacognitiveStallDetectionStatus,
    MetacognitiveStallDetector,
    MetacognitiveStallWindow,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
)


BASE = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
EVIDENCE = "journal:plan-stall-001"


class StallDetectionFixtures:
    @staticmethod
    def journal(
        *,
        step_a_events: int = 4,
        step_b_events: int = 0,
        complete_a: bool = False,
        complete_plan: bool = False,
    ) -> ExecutionJournal:
        journal = ExecutionJournal("plan-stall-001")
        journal.append(
            ExecutionEventType.PLAN_CREATED,
            BASE,
            payload={"project_id": "project-stall-001"},
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            BASE + timedelta(seconds=1),
        )
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            BASE + timedelta(seconds=2),
        )
        sequence_time = 3
        event_cycle = (
            ExecutionEventType.STEP_READY,
            ExecutionEventType.STEP_ASSIGNED,
            ExecutionEventType.STEP_STARTED,
            ExecutionEventType.STEP_BLOCKED,
            ExecutionEventType.STEP_FAILED,
        )
        for index in range(step_a_events):
            event_type = event_cycle[index % len(event_cycle)]
            kwargs = {"step_id": "step-a"}
            if event_type in {
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.STEP_FAILED,
            }:
                kwargs["agent_id"] = "BUILD_AGENT"
            journal.append(
                event_type,
                BASE + timedelta(seconds=sequence_time),
                **kwargs,
            )
            sequence_time += 1
        for index in range(step_b_events):
            event_type = event_cycle[index % len(event_cycle)]
            kwargs = {"step_id": "step-b"}
            if event_type in {
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
                ExecutionEventType.STEP_BLOCKED,
                ExecutionEventType.STEP_FAILED,
            }:
                kwargs["agent_id"] = "VERIFY_AGENT"
            journal.append(
                event_type,
                BASE + timedelta(seconds=sequence_time),
                **kwargs,
            )
            sequence_time += 1
        if complete_a:
            journal.append(
                ExecutionEventType.STEP_COMPLETED,
                BASE + timedelta(seconds=sequence_time),
                step_id="step-a",
                agent_id="BUILD_AGENT",
            )
            sequence_time += 1
        if complete_plan:
            journal.append(
                ExecutionEventType.PLAN_COMPLETED,
                BASE + timedelta(seconds=sequence_time),
            )
        return journal

    @staticmethod
    def context(journal: ExecutionJournal) -> MetacognitiveSupervisionContext:
        seal = journal.seal()
        return MetacognitiveSupervisionContext.capture(
            plan_id=journal.plan_id,
            project_id="project-stall-001",
            plan_state_hash="1" * 64,
            journal_hash=seal.journal_hash,
            checkpoint_hash="2" * 64,
            evidence_references=(EVIDENCE,),
            observed_by="SUPERVISOR_AGENT",
            observed_at=BASE + timedelta(minutes=1),
            objective="Detect active orchestration stalls.",
        )

    @staticmethod
    def policy(**overrides: object) -> MetacognitiveStallDetectionPolicy:
        values = {
            "policy_id": "stall-policy-v1",
            "minimum_activity_events": 4,
            "maximum_window_events": 64,
            "minimum_sequence_span": 3,
            "high_risk_activity_events": 7,
            "critical_risk_activity_events": 10,
            "base_confidence_bp": 6500,
            "activity_confidence_increment_bp": 350,
            "include_plan_events": False,
            "fail_closed": True,
        }
        values.update(overrides)
        return MetacognitiveStallDetectionPolicy(**values)

    @classmethod
    def request(
        cls,
        journal: ExecutionJournal,
        *,
        policy: MetacognitiveStallDetectionPolicy | None = None,
    ) -> MetacognitiveStallDetectionRequest:
        context = cls.context(journal)
        return MetacognitiveStallDetectionRequest.capture(
            policy=policy or cls.policy(),
            context=context,
            journal=journal,
            journal_evidence_reference=EVIDENCE,
            requested_by="SUPERVISOR_AGENT",
            requested_at=BASE + timedelta(minutes=2),
            reason="Inspect the current journal for non-progress activity.",
        )

    @classmethod
    def result(
        cls,
        journal: ExecutionJournal,
        *,
        policy: MetacognitiveStallDetectionPolicy | None = None,
    ) -> MetacognitiveStallDetectionResult:
        request = cls.request(journal, policy=policy)
        return MetacognitiveStallDetector().detect(
            request=request,
            journal=journal,
            completed_at=BASE + timedelta(minutes=3),
        )


class TestMetacognitiveStallDetectionPolicy(unittest.TestCase):
    def test_default_policy_is_fail_closed(self) -> None:
        policy = StallDetectionFixtures.policy()
        self.assertTrue(policy.fail_closed)
        self.assertEqual(policy.version, METACOGNITIVE_STALL_DETECTION_FORMAT_VERSION)

    def test_policy_round_trip_is_canonical(self) -> None:
        policy = StallDetectionFixtures.policy()
        restored = MetacognitiveStallDetectionPolicy.from_json(policy.to_json())
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_policy_hash_is_deterministic(self) -> None:
        first = StallDetectionFixtures.policy()
        second = StallDetectionFixtures.policy()
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_rejects_non_fail_closed_mode(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            StallDetectionFixtures.policy(fail_closed=False)

    def test_policy_rejects_maximum_below_minimum(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            StallDetectionFixtures.policy(
                minimum_activity_events=5,
                maximum_window_events=4,
            )

    def test_policy_rejects_high_threshold_below_minimum(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            StallDetectionFixtures.policy(
                minimum_activity_events=5,
                high_risk_activity_events=4,
            )

    def test_policy_rejects_critical_threshold_below_high(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            StallDetectionFixtures.policy(
                high_risk_activity_events=8,
                critical_risk_activity_events=7,
            )

    def test_policy_rejects_boolean_integer(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionError):
            StallDetectionFixtures.policy(minimum_activity_events=True)

    def test_policy_rejects_wrong_record_type(self) -> None:
        data = StallDetectionFixtures.policy().to_dict()
        data["record_type"] = "wrong"
        with self.assertRaises(MetacognitiveStallDetectionError):
            MetacognitiveStallDetectionPolicy.from_dict(data)

    def test_policy_rejects_invalid_json(self) -> None:
        with self.assertRaises(MetacognitiveStallDetectionError):
            MetacognitiveStallDetectionPolicy.from_json("{")


class TestMetacognitiveStallDetectionRequest(unittest.TestCase):
    def test_request_binds_policy_context_and_journal(self) -> None:
        journal = StallDetectionFixtures.journal()
        request = StallDetectionFixtures.request(journal)
        self.assertEqual(request.journal_plan_id, journal.plan_id)
        self.assertEqual(request.journal_event_count, journal.event_count)
        self.assertEqual(request.context_hash, request.context.context_hash)
        self.assertEqual(request.policy_hash, request.policy.policy_hash)

    def test_request_round_trip_verifies_hash(self) -> None:
        journal = StallDetectionFixtures.journal()
        request = StallDetectionFixtures.request(journal)
        restored = MetacognitiveStallDetectionRequest.from_json(request.to_json())
        restored.verify_hash()
        self.assertEqual(restored, request)

    def test_request_rejects_unbound_evidence(self) -> None:
        journal = StallDetectionFixtures.journal()
        context = StallDetectionFixtures.context(journal)
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallDetectionRequest.capture(
                policy=StallDetectionFixtures.policy(),
                context=context,
                journal=journal,
                journal_evidence_reference="journal:other",
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE + timedelta(minutes=2),
                reason="Invalid evidence binding.",
            )

    def test_request_rejects_time_before_context(self) -> None:
        journal = StallDetectionFixtures.journal()
        context = StallDetectionFixtures.context(journal)
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            MetacognitiveStallDetectionRequest.capture(
                policy=StallDetectionFixtures.policy(),
                context=context,
                journal=journal,
                journal_evidence_reference=EVIDENCE,
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE,
                reason="Invalid request time.",
            )

    def test_request_rejects_tampered_policy_hash(self) -> None:
        request = StallDetectionFixtures.request(StallDetectionFixtures.journal())
        data = request.to_dict()
        data["policy_hash"] = "f" * 64
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallDetectionRequest.from_dict(data)

    def test_request_rejects_tampered_request_hash(self) -> None:
        request = StallDetectionFixtures.request(StallDetectionFixtures.journal())
        data = request.to_dict()
        data["request_hash"] = "f" * 64
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallDetectionRequest.from_dict(data)


class TestMetacognitiveStallWindow(unittest.TestCase):
    def _window(self, event_count: int = 4) -> MetacognitiveStallWindow:
        journal = StallDetectionFixtures.journal(step_a_events=event_count)
        context = StallDetectionFixtures.context(journal)
        events = tuple(event for event in journal.events if event.step_id == "step-a")
        return MetacognitiveStallWindow.capture(
            context=context,
            journal_hash=journal.seal().journal_hash,
            step_id="step-a",
            events=events,
            risk_level=MetacognitiveRiskLevel.MEDIUM,
            confidence_bp=6500,
            evidence_references=(EVIDENCE,),
        )

    def test_window_round_trip(self) -> None:
        window = self._window()
        restored = MetacognitiveStallWindow.from_json(window.to_json())
        restored.verify_hash()
        self.assertEqual(restored, window)

    def test_window_preserves_event_order(self) -> None:
        window = self._window()
        self.assertEqual(
            window.event_types,
            (
                ExecutionEventType.STEP_READY,
                ExecutionEventType.STEP_ASSIGNED,
                ExecutionEventType.STEP_STARTED,
                ExecutionEventType.STEP_BLOCKED,
            ),
        )

    def test_window_collects_agent_ids(self) -> None:
        window = self._window()
        self.assertEqual(window.agent_ids, ("BUILD_AGENT",))

    def test_window_rejects_wrong_step_event(self) -> None:
        journal = StallDetectionFixtures.journal(
            step_a_events=3, step_b_events=1
        )
        context = StallDetectionFixtures.context(journal)
        events = tuple(event for event in journal.events if event.step_id is not None)
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallWindow.capture(
                context=context,
                journal_hash=journal.seal().journal_hash,
                step_id="step-a",
                events=events,
                risk_level=MetacognitiveRiskLevel.MEDIUM,
                confidence_bp=6500,
                evidence_references=(EVIDENCE,),
            )

    def test_window_rejects_progress_event(self) -> None:
        journal = StallDetectionFixtures.journal(
            step_a_events=3, complete_a=True
        )
        context = StallDetectionFixtures.context(journal)
        events = tuple(event for event in journal.events if event.step_id == "step-a")
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            MetacognitiveStallWindow.capture(
                context=context,
                journal_hash=journal.seal().journal_hash,
                step_id="step-a",
                events=events,
                risk_level=MetacognitiveRiskLevel.MEDIUM,
                confidence_bp=6500,
                evidence_references=(EVIDENCE,),
            )

    def test_window_rejects_low_risk(self) -> None:
        window = self._window()
        data = window.to_dict()
        data["risk_level"] = "low"
        data["window_id"] = "metacognitive-stall-window:" + "0" * 64
        data.pop("window_hash")
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            MetacognitiveStallWindow(**{
                "window_id": data["window_id"],
                "context_id": data["context_id"],
                "context_hash": data["context_hash"],
                "journal_hash": data["journal_hash"],
                "step_id": data["step_id"],
                "start_sequence": data["start_sequence"],
                "end_sequence": data["end_sequence"],
                "activity_event_count": data["activity_event_count"],
                "sequence_span": data["sequence_span"],
                "event_types": tuple(data["event_types"]),
                "agent_ids": tuple(data["agent_ids"]),
                "risk_level": data["risk_level"],
                "confidence_bp": data["confidence_bp"],
                "evidence_references": tuple(data["evidence_references"]),
            })

    def test_window_rejects_tampered_hash(self) -> None:
        window = self._window()
        data = window.to_dict()
        data["window_hash"] = "f" * 64
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallWindow.from_dict(data)


class TestMetacognitiveStallDetector(unittest.TestCase):
    def test_detects_minimum_stall(self) -> None:
        journal = StallDetectionFixtures.journal(step_a_events=4)
        result = StallDetectionFixtures.result(journal)
        self.assertEqual(
            result.status,
            MetacognitiveStallDetectionStatus.STALLS_DETECTED,
        )
        self.assertEqual(len(result.windows), 1)
        self.assertEqual(result.windows[0].step_id, "step-a")
        self.assertEqual(result.windows[0].risk_level, MetacognitiveRiskLevel.MEDIUM)

    def test_does_not_detect_below_threshold(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=3)
        )
        self.assertEqual(result.status, MetacognitiveStallDetectionStatus.CLEAR)
        self.assertEqual(result.records, ())

    def test_completed_step_is_not_stalled(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(
                step_a_events=5,
                complete_a=True,
            )
        )
        self.assertEqual(result.status, MetacognitiveStallDetectionStatus.CLEAR)

    def test_terminal_plan_clears_active_stalls(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(
                step_a_events=5,
                complete_plan=True,
            )
        )
        self.assertEqual(result.status, MetacognitiveStallDetectionStatus.CLEAR)

    def test_detects_two_independent_stalls(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(
                step_a_events=4,
                step_b_events=4,
            )
        )
        self.assertEqual(len(result.windows), 2)
        self.assertEqual(
            tuple(window.step_id for window in result.windows),
            ("step-a", "step-b"),
        )

    def test_high_risk_threshold(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=7)
        )
        self.assertEqual(
            result.windows[0].risk_level,
            MetacognitiveRiskLevel.HIGH,
        )

    def test_critical_risk_threshold(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=10)
        )
        self.assertEqual(
            result.windows[0].risk_level,
            MetacognitiveRiskLevel.CRITICAL,
        )

    def test_confidence_increases_deterministically(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=6)
        )
        self.assertEqual(result.windows[0].confidence_bp, 7200)

    def test_confidence_is_capped(self) -> None:
        policy = StallDetectionFixtures.policy(
            activity_confidence_increment_bp=4000
        )
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=10),
            policy=policy,
        )
        self.assertEqual(result.windows[0].confidence_bp, 10_000)

    def test_window_is_capped_to_policy_maximum(self) -> None:
        policy = StallDetectionFixtures.policy(maximum_window_events=5)
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=8),
            policy=policy,
        )
        self.assertEqual(result.windows[0].activity_event_count, 5)

    def test_minimum_sequence_span_is_enforced(self) -> None:
        policy = StallDetectionFixtures.policy(
            minimum_activity_events=2,
            minimum_sequence_span=5,
        )
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=2),
            policy=policy,
        )
        self.assertEqual(result.status, MetacognitiveStallDetectionStatus.CLEAR)

    def test_finding_kind_is_stall(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=4)
        )
        self.assertEqual(result.findings[0].kind, MetacognitiveFindingKind.STALL)

    def test_finding_is_bound_to_step_and_evidence(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=4)
        )
        finding = result.findings[0]
        self.assertEqual(finding.affected_step_ids, ("step-a",))
        self.assertEqual(finding.evidence_references, (EVIDENCE,))

    def test_result_round_trip(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=4)
        )
        restored = MetacognitiveStallDetectionResult.from_json(result.to_json())
        restored.verify_hash()
        self.assertEqual(restored, result)

    def test_record_round_trip(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=4)
        )
        record = result.records[0]
        restored = MetacognitiveStallDetectionRecord.from_json(record.to_json())
        restored.verify_hash()
        self.assertEqual(restored, record)

    def test_detection_is_deterministic(self) -> None:
        journal = StallDetectionFixtures.journal(step_a_events=6)
        request = StallDetectionFixtures.request(journal)
        detector = MetacognitiveStallDetector()
        first = detector.detect(
            request=request,
            journal=journal,
            completed_at=BASE + timedelta(minutes=3),
        )
        second = detector.detect(
            request=request,
            journal=journal,
            completed_at=BASE + timedelta(minutes=3),
        )
        self.assertEqual(first.to_json(), second.to_json())

    def test_detection_does_not_mutate_journal(self) -> None:
        journal = StallDetectionFixtures.journal(step_a_events=6)
        before = journal.to_jsonl()
        StallDetectionFixtures.result(journal)
        self.assertEqual(journal.to_jsonl(), before)

    def test_detection_rejects_mismatched_journal(self) -> None:
        original = StallDetectionFixtures.journal(step_a_events=4)
        request = StallDetectionFixtures.request(original)
        other = StallDetectionFixtures.journal(step_a_events=5)
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallDetector().detect(
                request=request,
                journal=other,
                completed_at=BASE + timedelta(minutes=3),
            )

    def test_detection_rejects_completion_before_request(self) -> None:
        journal = StallDetectionFixtures.journal(step_a_events=4)
        request = StallDetectionFixtures.request(journal)
        with self.assertRaises(MetacognitiveStallDetectionPolicyError):
            MetacognitiveStallDetector().detect(
                request=request,
                journal=journal,
                completed_at=BASE + timedelta(minutes=1),
            )

    def test_result_rejects_tampered_hash(self) -> None:
        result = StallDetectionFixtures.result(
            StallDetectionFixtures.journal(step_a_events=4)
        )
        data = result.to_dict()
        data["result_hash"] = "f" * 64
        with self.assertRaises(MetacognitiveStallDetectionIntegrityError):
            MetacognitiveStallDetectionResult.from_dict(data)


if __name__ == "__main__":
    unittest.main()
