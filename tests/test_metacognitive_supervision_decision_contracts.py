from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import unittest

from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveDecisionAction,
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecision,
    MetacognitiveSupervisionDecisionError,
    MetacognitiveSupervisionDecisionIntegrityError,
    MetacognitiveSupervisionDecisionPolicy,
    MetacognitiveSupervisionDecisionPolicyError,
    MetacognitiveSupervisionFinding,
)


OBSERVED_AT = "2026-08-06T03:42:00Z"
DECIDED_AT = "2026-08-06T03:43:00Z"


def digest(character: str) -> str:
    return character * 64


def make_policy(**changes):
    values = {
        "policy_id": "policy:metacognitive-supervision-v1",
        "minimum_continue_confidence_bp": 7000,
        "minimum_correct_confidence_bp": 5000,
        "require_approval_for_pause": True,
        "require_approval_for_stop": True,
        "require_approval_for_escalate": True,
        "fail_closed": True,
    }
    values.update(changes)
    return MetacognitiveSupervisionDecisionPolicy(**values)


def make_context(**changes):
    values = {
        "plan_id": "plan:metacognitive-001",
        "project_id": "project:elman-001",
        "plan_state_hash": digest("a"),
        "journal_hash": digest("b"),
        "checkpoint_hash": digest("c"),
        "evidence_references": (
            "evidence:checkpoint-001",
            "evidence:journal-001",
        ),
        "observed_by": "ELMAN_SUPERVISOR",
        "observed_at": OBSERVED_AT,
        "objective": "Evaluate orchestration state before continuation.",
    }
    values.update(changes)
    return MetacognitiveSupervisionContext.capture(**values)


def make_finding(
    context=None,
    *,
    kind=MetacognitiveFindingKind.UNCERTAINTY,
    risk=MetacognitiveRiskLevel.MEDIUM,
    summary="Evidence confidence is insufficient.",
    evidence=("evidence:journal-001",),
    steps=("step.1",),
    confidence=7500,
):
    effective_context = context or make_context()
    return MetacognitiveSupervisionFinding.from_context(
        context=effective_context,
        kind=kind,
        risk_level=risk,
        summary=summary,
        evidence_references=evidence,
        affected_step_ids=steps,
        confidence_bp=confidence,
    )


def make_decision(
    *,
    policy=None,
    context=None,
    findings=(),
    action=MetacognitiveDecisionAction.CONTINUE,
    confidence=8000,
    approval_required=False,
    approval_reference=None,
    corrective_steps=(),
    decided_at=DECIDED_AT,
):
    effective_policy = policy or make_policy()
    effective_context = context or make_context()
    return MetacognitiveSupervisionDecision.declare(
        policy=effective_policy,
        context=effective_context,
        findings=findings,
        action=action,
        confidence_bp=confidence,
        approval_required=approval_required,
        approval_reference=approval_reference,
        corrective_step_ids=corrective_steps,
        decided_by="ELMAN_SUPERVISOR",
        decided_at=decided_at,
        rationale="Deterministic metacognitive supervision decision.",
    )


class PolicyTests(unittest.TestCase):
    def test_policy_hash_is_deterministic(self):
        self.assertEqual(make_policy().policy_hash, make_policy().policy_hash)

    def test_policy_json_round_trip(self):
        policy = make_policy(minimum_continue_confidence_bp=8000)
        self.assertEqual(
            MetacognitiveSupervisionDecisionPolicy.from_json(policy.to_json()),
            policy,
        )

    def test_policy_rejects_disabled_fail_closed(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_policy(fail_closed=False)

    def test_policy_rejects_inverted_thresholds(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_policy(
                minimum_continue_confidence_bp=4000,
                minimum_correct_confidence_bp=5000,
            )

    def test_policy_rejects_non_boolean(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_policy(require_approval_for_pause="yes")

    def test_policy_is_frozen(self):
        policy = make_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.fail_closed = False  # type: ignore[misc]


class ContextTests(unittest.TestCase):
    def test_context_identifier_is_deterministic(self):
        self.assertEqual(make_context().context_id, make_context().context_id)

    def test_context_json_round_trip(self):
        context = make_context()
        restored = MetacognitiveSupervisionContext.from_json(context.to_json())
        self.assertEqual(restored, context)
        restored.verify_hash()

    def test_context_sorts_and_deduplicates_evidence(self):
        context = make_context(
            evidence_references=(
                "evidence:journal-001",
                "evidence:checkpoint-001",
                "evidence:journal-001",
            )
        )
        self.assertEqual(
            context.evidence_references,
            ("evidence:checkpoint-001", "evidence:journal-001"),
        )

    def test_context_requires_evidence(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_context(evidence_references=())

    def test_context_rejects_invalid_agent(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_context(observed_by="human")

    def test_context_rejects_non_utc_datetime(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_context(
                observed_at=datetime(
                    2026,
                    8,
                    6,
                    tzinfo=timezone(timedelta(hours=1)),
                )
            )

    def test_context_accepts_utc_datetime(self):
        context = make_context(
            observed_at=datetime(2026, 8, 6, 3, 42, tzinfo=UTC)
        )
        self.assertEqual(context.observed_at, "2026-08-06T03:42:00.000000Z")

    def test_context_rejects_tampered_hash(self):
        data = make_context().to_dict()
        data["objective"] = "Tampered"
        with self.assertRaises(
            MetacognitiveSupervisionDecisionIntegrityError
        ):
            MetacognitiveSupervisionContext.from_dict(data)


class FindingTests(unittest.TestCase):
    def test_finding_identifier_is_deterministic(self):
        context = make_context()
        self.assertEqual(
            make_finding(context).finding_id,
            make_finding(context).finding_id,
        )

    def test_finding_json_round_trip(self):
        finding = make_finding()
        restored = MetacognitiveSupervisionFinding.from_json(
            finding.to_json()
        )
        self.assertEqual(restored, finding)
        restored.verify_hash()

    def test_finding_requires_bound_evidence(self):
        with self.assertRaises(
            MetacognitiveSupervisionDecisionIntegrityError
        ):
            make_finding(evidence=("evidence:unknown",))

    def test_finding_requires_evidence(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_finding(evidence=())

    def test_finding_rejects_confidence_outside_range(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionError):
            make_finding(confidence=10001)

    def test_finding_sorts_step_identifiers(self):
        finding = make_finding(steps=("step.2", "step.1", "step.2"))
        self.assertEqual(finding.affected_step_ids, ("step.1", "step.2"))

    def test_finding_rejects_tampered_hash(self):
        data = make_finding().to_dict()
        data["summary"] = "Tampered"
        with self.assertRaises(
            MetacognitiveSupervisionDecisionIntegrityError
        ):
            MetacognitiveSupervisionFinding.from_dict(data)

    def test_finding_is_frozen(self):
        finding = make_finding()
        with self.assertRaises(FrozenInstanceError):
            finding.summary = "other"  # type: ignore[misc]


class DecisionTests(unittest.TestCase):
    def test_continue_without_findings_is_valid(self):
        decision = make_decision()
        self.assertIs(decision.action, MetacognitiveDecisionAction.CONTINUE)
        self.assertIs(
            decision.highest_risk,
            MetacognitiveRiskLevel.INFO,
        )

    def test_continue_accepts_low_risk(self):
        context = make_context()
        finding = make_finding(
            context,
            risk=MetacognitiveRiskLevel.LOW,
        )
        decision = make_decision(context=context, findings=(finding,))
        self.assertIs(decision.action, MetacognitiveDecisionAction.CONTINUE)

    def test_continue_rejects_medium_risk(self):
        context = make_context()
        finding = make_finding(context)
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(context=context, findings=(finding,))

    def test_continue_rejects_low_confidence(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(confidence=6999)

    def test_correct_is_valid_with_corrective_steps(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.CORRECT,
            confidence=7000,
            corrective_steps=("step.1",),
        )
        self.assertEqual(decision.corrective_step_ids, ("step.1",))

    def test_correct_requires_finding(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                action=MetacognitiveDecisionAction.CORRECT,
                corrective_steps=("step.1",),
            )

    def test_correct_requires_corrective_steps(self):
        context = make_context()
        finding = make_finding(context)
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.CORRECT,
                confidence=7000,
            )

    def test_correct_rejects_critical_risk(self):
        context = make_context()
        finding = make_finding(
            context,
            risk=MetacognitiveRiskLevel.CRITICAL,
        )
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.CORRECT,
                corrective_steps=("step.1",),
            )

    def test_pause_is_valid_for_medium_risk(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
        )
        self.assertTrue(decision.approval_required)

    def test_pause_rejects_low_risk(self):
        context = make_context()
        finding = make_finding(
            context,
            risk=MetacognitiveRiskLevel.LOW,
        )
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.PAUSE,
                approval_required=True,
            )

    def test_stop_is_valid_for_high_risk(self):
        context = make_context()
        finding = make_finding(
            context,
            risk=MetacognitiveRiskLevel.HIGH,
        )
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.STOP,
            approval_required=True,
        )
        self.assertIs(decision.action, MetacognitiveDecisionAction.STOP)

    def test_stop_rejects_medium_risk(self):
        context = make_context()
        finding = make_finding(context)
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.STOP,
                approval_required=True,
            )

    def test_escalate_is_valid_for_medium_risk(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.ESCALATE,
            approval_required=True,
        )
        self.assertIs(
            decision.action,
            MetacognitiveDecisionAction.ESCALATE,
        )

    def test_critical_requires_stop_or_escalate(self):
        context = make_context()
        finding = make_finding(
            context,
            risk=MetacognitiveRiskLevel.CRITICAL,
        )
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.PAUSE,
                approval_required=True,
            )

    def test_decision_identifier_is_deterministic(self):
        self.assertEqual(
            make_decision().decision_id,
            make_decision().decision_id,
        )

    def test_decision_json_round_trip(self):
        decision = make_decision()
        restored = MetacognitiveSupervisionDecision.from_json(
            decision.to_json()
        )
        self.assertEqual(restored, decision)
        restored.verify_hash()

    def test_decision_rejects_tampered_hash(self):
        data = make_decision().to_dict()
        data["rationale"] = "Tampered"
        with self.assertRaises(
            MetacognitiveSupervisionDecisionIntegrityError
        ):
            MetacognitiveSupervisionDecision.from_dict(data)

    def test_decision_rejects_finding_from_other_context(self):
        context = make_context()
        other = make_context(
            plan_state_hash=digest("d"),
        )
        finding = make_finding(other)
        with self.assertRaises(
            MetacognitiveSupervisionDecisionIntegrityError
        ):
            make_decision(
                context=context,
                findings=(finding,),
                action=MetacognitiveDecisionAction.PAUSE,
                approval_required=True,
            )

    def test_decision_rejects_time_before_observation(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(decided_at="2026-08-06T03:41:59Z")

    def test_approval_reference_requires_approval(self):
        with self.assertRaises(MetacognitiveSupervisionDecisionPolicyError):
            make_decision(approval_reference="approval:001")

    def test_source_objects_remain_unchanged(self):
        policy = make_policy()
        context = make_context()
        finding = make_finding(context)
        before = (policy.to_json(), context.to_json(), finding.to_json())
        make_decision(
            policy=policy,
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
        )
        after = (policy.to_json(), context.to_json(), finding.to_json())
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
