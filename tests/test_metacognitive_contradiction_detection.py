from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from elman_os.execution_journal import ExecutionEventType, ExecutionJournal
from elman_os.metacognitive_contradiction_detection import (
    MetacognitiveAssertion,
    MetacognitiveContradiction,
    MetacognitiveContradictionDetectionError,
    MetacognitiveContradictionDetectionIntegrityError,
    MetacognitiveContradictionDetectionPolicy,
    MetacognitiveContradictionDetectionPolicyError,
    MetacognitiveContradictionDetectionRequest,
    MetacognitiveContradictionDetectionResult,
    MetacognitiveContradictionDetectionStatus,
    MetacognitiveContradictionDetector,
    MetacognitiveContradictionKind,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
)


T0 = "2026-08-06T00:00:00.000000Z"
T1 = "2026-08-06T00:00:01.000000Z"
T2 = "2026-08-06T00:00:02.000000Z"
T3 = "2026-08-06T00:00:03.000000Z"
T4 = "2026-08-06T00:00:04.000000Z"
T5 = "2026-08-06T00:00:05.000000Z"


class ContradictionDetectionTestCase(unittest.TestCase):
    def make_journal(self) -> ExecutionJournal:
        journal = ExecutionJournal("plan:contradiction-demo")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(ExecutionEventType.PLAN_STARTED, T1)
        return journal

    def make_context(
        self,
        journal: ExecutionJournal,
    ) -> MetacognitiveSupervisionContext:
        seal = journal.seal()
        return MetacognitiveSupervisionContext.capture(
            plan_id=journal.plan_id,
            project_id="project:contradiction-demo",
            plan_state_hash="1" * 64,
            journal_hash=seal.journal_hash,
            checkpoint_hash="2" * 64,
            evidence_references=("journal:primary", "checkpoint:primary"),
            observed_by="META_SUPERVISOR",
            observed_at=T4,
            objective="Detect explicit contradictions without mutation.",
        )

    def make_request(
        self,
        journal: ExecutionJournal,
        *,
        policy: MetacognitiveContradictionDetectionPolicy | None = None,
    ) -> MetacognitiveContradictionDetectionRequest:
        context = self.make_context(journal)
        return MetacognitiveContradictionDetectionRequest.create(
            policy=policy
            or MetacognitiveContradictionDetectionPolicy(
                policy_id="policy:contradiction-default"
            ),
            context=context,
            journal=journal,
            journal_evidence_reference="journal:primary",
            requested_by="META_SUPERVISOR",
            requested_at=T5,
            reason="Run deterministic contradiction detection.",
        )

    def detect(
        self,
        journal: ExecutionJournal,
        *,
        policy: MetacognitiveContradictionDetectionPolicy | None = None,
    ) -> MetacognitiveContradictionDetectionResult:
        return MetacognitiveContradictionDetector().detect(
            request=self.make_request(journal, policy=policy),
            journal=journal,
            analyzed_by="META_SUPERVISOR",
        )

    def test_policy_is_hash_bound_and_round_trips(self) -> None:
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:roundtrip"
        )
        restored = MetacognitiveContradictionDetectionPolicy.from_json(
            policy.to_json()
        )
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_policy_rejects_fail_open(self) -> None:
        with self.assertRaises(
            MetacognitiveContradictionDetectionPolicyError
        ):
            MetacognitiveContradictionDetectionPolicy(
                policy_id="policy:unsafe",
                fail_closed=False,
            )

    def test_policy_requires_one_enabled_detector(self) -> None:
        with self.assertRaises(
            MetacognitiveContradictionDetectionPolicyError
        ):
            MetacognitiveContradictionDetectionPolicy(
                policy_id="policy:none",
                include_assertion_conflicts=False,
                include_completed_step_regressions=False,
            )

    def test_policy_rejects_inverted_risk_thresholds(self) -> None:
        with self.assertRaises(
            MetacognitiveContradictionDetectionPolicyError
        ):
            MetacognitiveContradictionDetectionPolicy(
                policy_id="policy:thresholds",
                high_risk_distinct_values=4,
                critical_risk_distinct_values=3,
            )

    def test_policy_is_immutable(self) -> None:
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:immutable"
        )
        with self.assertRaises(FrozenInstanceError):
            policy.policy_id = "policy:changed"  # type: ignore[misc]

    def test_request_is_hash_bound_and_round_trips(self) -> None:
        journal = self.make_journal()
        request = self.make_request(journal)
        restored = MetacognitiveContradictionDetectionRequest.from_json(
            request.to_json()
        )
        self.assertEqual(restored, request)
        restored.verify_hash()

    def test_request_rejects_unbound_evidence_reference(self) -> None:
        journal = self.make_journal()
        context = self.make_context(journal)
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:evidence"
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradictionDetectionRequest.create(
                policy=policy,
                context=context,
                journal=journal,
                journal_evidence_reference="journal:unknown",
                requested_by="META_SUPERVISOR",
                requested_at=T5,
                reason="Invalid evidence binding.",
            )

    def test_request_rejects_tampered_policy_hash(self) -> None:
        journal = self.make_journal()
        data = json.loads(self.make_request(journal).to_json())
        data["policy_hash"] = "f" * 64
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradictionDetectionRequest.from_dict(data)

    def test_clear_journal_produces_clear_result(self) -> None:
        journal = self.make_journal()
        result = self.detect(journal)
        self.assertIs(
            result.status,
            MetacognitiveContradictionDetectionStatus.CLEAR,
        )
        self.assertEqual(result.assertions, ())
        self.assertEqual(result.contradictions, ())
        self.assertEqual(result.findings, ())

    def test_identical_assertions_are_not_contradictory(self) -> None:
        journal = ExecutionJournal("plan:identical-assertions")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        payload = {
            "metacognitive_assertions": [
                {
                    "scope": "scope:release",
                    "subject": "artifact:bundle",
                    "predicate": "approved",
                    "value": True,
                }
            ]
        }
        journal.append(ExecutionEventType.PLAN_STARTED, T1, payload=payload)
        journal.append(ExecutionEventType.PLAN_APPROVED, T2, payload=payload)
        result = self.detect(journal)
        self.assertEqual(len(result.assertions), 2)
        self.assertEqual(result.contradictions, ())

    def test_different_scopes_are_not_contradictory(self) -> None:
        journal = ExecutionJournal("plan:different-scopes")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:before-review",
                        "subject": "artifact:bundle",
                        "predicate": "approved",
                        "value": False,
                    }
                ]
            },
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T2,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:after-review",
                        "subject": "artifact:bundle",
                        "predicate": "approved",
                        "value": True,
                    }
                ]
            },
        )
        result = self.detect(journal)
        self.assertEqual(result.contradictions, ())

    def test_two_values_in_same_scope_create_contradiction(self) -> None:
        journal = ExecutionJournal("plan:assertion-conflict")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:release",
                        "subject": "artifact:bundle",
                        "predicate": "approved",
                        "value": False,
                    }
                ]
            },
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T2,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:release",
                        "subject": "artifact:bundle",
                        "predicate": "approved",
                        "value": True,
                    }
                ]
            },
        )
        result = self.detect(journal)
        self.assertIs(
            result.status,
            MetacognitiveContradictionDetectionStatus.CONTRADICTIONS_DETECTED,
        )
        self.assertEqual(len(result.contradictions), 1)
        contradiction = result.contradictions[0]
        self.assertIs(
            contradiction.kind,
            MetacognitiveContradictionKind.ASSERTION_CONFLICT,
        )
        self.assertEqual(contradiction.scope, "scope:release")
        self.assertEqual(
            contradiction.values_json,
            ("false", "true"),
        )
        self.assertIs(
            result.findings[0].kind,
            MetacognitiveFindingKind.CONTRADICTION,
        )

    def test_three_values_raise_risk_to_high(self) -> None:
        journal = ExecutionJournal("plan:three-values")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        values = ("draft", "review", "approved")
        times = (T1, T2, T3)
        event_types = (
            ExecutionEventType.PLAN_STARTED,
            ExecutionEventType.PLAN_APPROVED,
            ExecutionEventType.PLAN_BLOCKED,
        )
        for value, timestamp, event_type in zip(
            values, times, event_types, strict=True
        ):
            journal.append(
                event_type,
                timestamp,
                payload={
                    "metacognitive_assertions": [
                        {
                            "scope": "scope:release",
                            "subject": "artifact:bundle",
                            "predicate": "state",
                            "value": value,
                        }
                    ]
                },
            )
        result = self.detect(journal)
        self.assertIs(
            result.contradictions[0].risk_level,
            MetacognitiveRiskLevel.HIGH,
        )

    def test_four_values_raise_risk_to_critical(self) -> None:
        journal = ExecutionJournal("plan:four-values")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        entries = (
            (ExecutionEventType.PLAN_STARTED, T1, "draft"),
            (ExecutionEventType.PLAN_APPROVED, T2, "review"),
            (ExecutionEventType.PLAN_BLOCKED, T3, "blocked"),
            (ExecutionEventType.PLAN_STARTED, T4, "approved"),
        )
        for event_type, timestamp, value in entries:
            journal.append(
                event_type,
                timestamp,
                payload={
                    "metacognitive_assertions": [
                        {
                            "scope": "scope:release",
                            "subject": "artifact:bundle",
                            "predicate": "state",
                            "value": value,
                        }
                    ]
                },
            )
        result = self.detect(journal)
        self.assertIs(
            result.contradictions[0].risk_level,
            MetacognitiveRiskLevel.CRITICAL,
        )

    def test_canonical_json_prevents_key_order_false_positive(self) -> None:
        journal = ExecutionJournal("plan:canonical-values")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:config",
                        "subject": "config:runtime",
                        "predicate": "value",
                        "value": {"a": 1, "b": 2},
                    }
                ]
            },
        )
        journal.append(
            ExecutionEventType.PLAN_APPROVED,
            T2,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:config",
                        "subject": "config:runtime",
                        "predicate": "value",
                        "value": {"b": 2, "a": 1},
                    }
                ]
            },
        )
        result = self.detect(journal)
        self.assertEqual(result.contradictions, ())

    def test_assertion_step_id_is_inherited_from_event(self) -> None:
        journal = ExecutionJournal("plan:step-inheritance")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        event = journal.append(
            ExecutionEventType.STEP_STARTED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:build",
                        "subject": "artifact:binary",
                        "predicate": "valid",
                        "value": True,
                    }
                ]
            },
        )
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:inheritance"
        )
        from elman_os.metacognitive_contradiction_detection import (
            _assertions_from_event,
        )
        assertion = _assertions_from_event(event, policy)[0]
        self.assertEqual(assertion.step_id, "build")

    def test_assertion_rejects_step_id_mismatch(self) -> None:
        journal = ExecutionJournal("plan:step-mismatch")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_STARTED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:build",
                        "subject": "artifact:binary",
                        "predicate": "valid",
                        "value": True,
                        "step_id": "test",
                    }
                ]
            },
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            self.detect(journal)

    def test_malformed_assertion_envelope_fails_closed(self) -> None:
        journal = ExecutionJournal("plan:bad-envelope")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={"metacognitive_assertions": {"not": "an-array"}},
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            self.detect(journal)

    def test_missing_assertion_field_fails_closed(self) -> None:
        journal = ExecutionJournal("plan:missing-field")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:release",
                        "subject": "artifact:bundle",
                        "value": True,
                    }
                ]
            },
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            self.detect(journal)

    def test_unknown_assertion_field_fails_closed(self) -> None:
        journal = ExecutionJournal("plan:unknown-field")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={
                "metacognitive_assertions": [
                    {
                        "scope": "scope:release",
                        "subject": "artifact:bundle",
                        "predicate": "approved",
                        "value": True,
                        "unexpected": "field",
                    }
                ]
            },
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            self.detect(journal)

    def test_assertion_limit_is_enforced(self) -> None:
        journal = ExecutionJournal("plan:assertion-limit")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        assertions = [
            {
                "scope": "scope:release",
                "subject": f"artifact:item-{index}",
                "predicate": "approved",
                "value": True,
            }
            for index in range(3)
        ]
        journal.append(
            ExecutionEventType.PLAN_STARTED,
            T1,
            payload={"metacognitive_assertions": assertions},
        )
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:limit",
            maximum_assertions_per_event=2,
        )
        with self.assertRaises(
            MetacognitiveContradictionDetectionPolicyError
        ):
            self.detect(journal, policy=policy)

    def test_blocked_before_completed_is_not_regression(self) -> None:
        journal = ExecutionJournal("plan:normal-recovery")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T2,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        result = self.detect(journal)
        self.assertEqual(result.contradictions, ())

    def test_failed_before_completed_is_not_regression(self) -> None:
        journal = ExecutionJournal("plan:retry-success")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_FAILED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T2,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        result = self.detect(journal)
        self.assertEqual(result.contradictions, ())

    def test_blocked_after_completed_is_regression(self) -> None:
        journal = ExecutionJournal("plan:blocked-regression")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        journal.append(
            ExecutionEventType.STEP_BLOCKED,
            T2,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        result = self.detect(journal)
        contradiction = result.contradictions[0]
        self.assertIs(
            contradiction.kind,
            MetacognitiveContradictionKind.COMPLETED_STEP_REGRESSION,
        )
        self.assertEqual(contradiction.affected_step_ids, ("build",))
        self.assertIs(
            contradiction.risk_level,
            MetacognitiveRiskLevel.HIGH,
        )

    def test_failed_after_completed_is_regression(self) -> None:
        journal = ExecutionJournal("plan:failed-regression")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        journal.append(
            ExecutionEventType.STEP_FAILED,
            T2,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        result = self.detect(journal)
        self.assertEqual(len(result.contradictions), 1)
        self.assertEqual(
            result.contradictions[0].values_json,
            ('"completed"', '"failed"'),
        )

    def test_lifecycle_detector_can_be_disabled(self) -> None:
        journal = ExecutionJournal("plan:disable-lifecycle")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        journal.append(
            ExecutionEventType.STEP_COMPLETED,
            T1,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        journal.append(
            ExecutionEventType.STEP_FAILED,
            T2,
            step_id="build",
            agent_id="BUILD_AGENT",
        )
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:no-lifecycle",
            include_completed_step_regressions=False,
        )
        result = self.detect(journal, policy=policy)
        self.assertEqual(result.contradictions, ())

    def test_assertion_detector_can_be_disabled(self) -> None:
        journal = ExecutionJournal("plan:disable-assertions")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        for timestamp, value in ((T1, False), (T2, True)):
            journal.append(
                ExecutionEventType.PLAN_STARTED,
                timestamp,
                payload={
                    "metacognitive_assertions": [
                        {
                            "scope": "scope:release",
                            "subject": "artifact:bundle",
                            "predicate": "approved",
                            "value": value,
                        }
                    ]
                },
            )
        policy = MetacognitiveContradictionDetectionPolicy(
            policy_id="policy:no-assertions",
            include_assertion_conflicts=False,
        )
        result = self.detect(journal, policy=policy)
        self.assertEqual(result.assertions, ())
        self.assertEqual(result.contradictions, ())

    def test_detector_is_deterministic(self) -> None:
        journal = ExecutionJournal("plan:deterministic")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        for timestamp, value in ((T1, False), (T2, True)):
            journal.append(
                ExecutionEventType.PLAN_STARTED,
                timestamp,
                payload={
                    "metacognitive_assertions": [
                        {
                            "scope": "scope:release",
                            "subject": "artifact:bundle",
                            "predicate": "approved",
                            "value": value,
                        }
                    ]
                },
            )
        request = self.make_request(journal)
        detector = MetacognitiveContradictionDetector()
        first = detector.detect(
            request=request,
            journal=journal,
            analyzed_by="META_SUPERVISOR",
        )
        second = detector.detect(
            request=request,
            journal=journal,
            analyzed_by="META_SUPERVISOR",
        )
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.result_hash, second.result_hash)

    def test_detector_does_not_mutate_journal_or_context(self) -> None:
        journal = self.make_journal()
        request = self.make_request(journal)
        before_journal = journal.to_jsonl()
        before_context = request.context.to_json()
        MetacognitiveContradictionDetector().detect(
            request=request,
            journal=journal,
            analyzed_by="META_SUPERVISOR",
        )
        self.assertEqual(journal.to_jsonl(), before_journal)
        self.assertEqual(request.context.to_json(), before_context)

    def test_detector_rejects_journal_different_from_request(self) -> None:
        journal = self.make_journal()
        request = self.make_request(journal)
        other = self.make_journal()
        other.append(ExecutionEventType.PLAN_APPROVED, T2)
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradictionDetector().detect(
                request=request,
                journal=other,
                analyzed_by="META_SUPERVISOR",
            )

    def test_result_round_trips_and_verifies(self) -> None:
        journal = self.make_journal()
        result = self.detect(journal)
        restored = MetacognitiveContradictionDetectionResult.from_json(
            result.to_json()
        )
        self.assertEqual(restored, result)
        restored.verify_hash()

    def test_result_rejects_tampered_status(self) -> None:
        journal = self.make_journal()
        result = self.detect(journal)
        data = json.loads(result.to_json())
        data["status"] = "contradictions-detected"
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradictionDetectionResult.from_dict(data)

    def test_result_rejects_tampered_hash(self) -> None:
        journal = self.make_journal()
        data = json.loads(self.detect(journal).to_json())
        data["result_hash"] = "f" * 64
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradictionDetectionResult.from_dict(data)

    def test_assertion_round_trip_and_tamper_detection(self) -> None:
        journal = self.make_journal()
        event = journal.events[-1]
        assertion = MetacognitiveAssertion.capture(
            scope="scope:release",
            subject="artifact:bundle",
            predicate="approved",
            value=True,
            event=event,
        )
        restored = MetacognitiveAssertion.from_json(assertion.to_json())
        self.assertEqual(restored, assertion)
        data = json.loads(assertion.to_json())
        data["value_json"] = "false"
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveAssertion.from_dict(data)

    def test_contradiction_round_trip_and_tamper_detection(self) -> None:
        journal = self.make_journal()
        events = journal.events
        contradiction = MetacognitiveContradiction.create(
            kind=MetacognitiveContradictionKind.ASSERTION_CONFLICT,
            scope="scope:release",
            subject="artifact:bundle",
            predicate="approved",
            values_json=("false", "true"),
            source_event_sequences=(events[0].sequence, events[1].sequence),
            source_event_hashes=(
                events[0].event_hash or "",
                events[1].event_hash or "",
            ),
            risk_level=MetacognitiveRiskLevel.MEDIUM,
            confidence_bp=7500,
        )
        restored = MetacognitiveContradiction.from_json(
            contradiction.to_json()
        )
        self.assertEqual(restored, contradiction)
        data = json.loads(contradiction.to_json())
        data["confidence_bp"] = 100
        with self.assertRaises(
            MetacognitiveContradictionDetectionIntegrityError
        ):
            MetacognitiveContradiction.from_dict(data)

    def test_each_contradiction_has_one_bound_finding(self) -> None:
        journal = ExecutionJournal("plan:bound-findings")
        journal.append(ExecutionEventType.PLAN_CREATED, T0)
        for timestamp, value in ((T1, False), (T2, True)):
            journal.append(
                ExecutionEventType.PLAN_STARTED,
                timestamp,
                payload={
                    "metacognitive_assertions": [
                        {
                            "scope": "scope:release",
                            "subject": "artifact:bundle",
                            "predicate": "approved",
                            "value": value,
                        }
                    ]
                },
            )
        result = self.detect(journal)
        self.assertEqual(
            len(result.contradictions),
            len(result.findings),
        )
        finding = result.findings[0]
        self.assertEqual(
            finding.context_hash,
            result.request.context_hash,
        )
        self.assertEqual(
            finding.evidence_references,
            ("journal:primary",),
        )

    def test_analyzed_at_is_deterministically_bound_to_request(self) -> None:
        journal = self.make_journal()
        result = self.detect(journal)
        self.assertEqual(
            result.analyzed_at,
            result.request.requested_at,
        )


if __name__ == "__main__":
    unittest.main()
