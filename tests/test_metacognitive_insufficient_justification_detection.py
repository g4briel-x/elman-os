from dataclasses import FrozenInstanceError, replace
import json
import unittest

from elman_os.metacognitive_insufficient_justification_detection import (
    MetacognitiveInsufficientJustificationDetectionIntegrityError,
    MetacognitiveInsufficientJustificationDetectionPolicy,
    MetacognitiveInsufficientJustificationDetectionPolicyError,
    MetacognitiveInsufficientJustificationDetectionRequest,
    MetacognitiveInsufficientJustificationDetectionResult,
    MetacognitiveInsufficientJustificationDetectionStatus,
    MetacognitiveJustificationGapKind,
    detect_insufficient_justification,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveDecisionAction,
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecision,
    MetacognitiveSupervisionDecisionPolicy,
    MetacognitiveSupervisionFinding,
)


OBSERVED_AT = "2026-08-07T03:00:00Z"
DECIDED_AT = "2026-08-07T03:01:00Z"
REQUESTED_AT = "2026-08-07T03:02:00Z"


def digest(character: str) -> str:
    return character * 64


def make_context():
    return MetacognitiveSupervisionContext.capture(
        plan_id="plan:justification-001",
        project_id="project:elman-001",
        plan_state_hash=digest("a"),
        journal_hash=digest("b"),
        checkpoint_hash=digest("c"),
        evidence_references=(
            "evidence:checkpoint-001",
            "evidence:journal-001",
        ),
        observed_by="ELMAN_SUPERVISOR",
        observed_at=OBSERVED_AT,
        objective="Evaluate whether the supervision decision is justified.",
    )


def make_decision_policy():
    return MetacognitiveSupervisionDecisionPolicy(
        policy_id="policy:metacognitive-supervision-v1",
        minimum_continue_confidence_bp=7000,
        minimum_correct_confidence_bp=5000,
        require_approval_for_pause=True,
        require_approval_for_stop=True,
        require_approval_for_escalate=True,
        fail_closed=True,
    )


def make_finding(context, *, risk=MetacognitiveRiskLevel.MEDIUM):
    return MetacognitiveSupervisionFinding.from_context(
        context=context,
        kind=MetacognitiveFindingKind.UNCERTAINTY,
        risk_level=risk,
        summary="Evidence requires explicit review.",
        evidence_references=("evidence:journal-001",),
        affected_step_ids=("step.1",),
        confidence_bp=8000,
    )


def make_detection_policy(**changes):
    values = {
        "policy_id": "policy:insufficient-justification-v1",
        "minimum_rationale_characters": 48,
        "minimum_cited_evidence_references": 1,
        "require_all_finding_citations": True,
        "require_corrective_step_citations": True,
        "require_approval_reference_when_required": True,
        "require_approval_reference_citation": True,
        "finding_confidence_bp": 9500,
        "fail_closed": True,
    }
    values.update(changes)
    return MetacognitiveInsufficientJustificationDetectionPolicy(**values)


def make_decision(
    *,
    context=None,
    findings=(),
    action=MetacognitiveDecisionAction.CONTINUE,
    confidence=8000,
    approval_required=False,
    approval_reference=None,
    corrective_steps=(),
    rationale=None,
):
    effective_context = context or make_context()
    effective_rationale = rationale or (
        "Decision is supported by evidence:checkpoint-001 and remains "
        "within the deterministic supervision policy."
    )
    return MetacognitiveSupervisionDecision.declare(
        policy=make_decision_policy(),
        context=effective_context,
        findings=findings,
        action=action,
        confidence_bp=confidence,
        approval_required=approval_required,
        approval_reference=approval_reference,
        corrective_step_ids=corrective_steps,
        decided_by="ELMAN_SUPERVISOR",
        decided_at=DECIDED_AT,
        rationale=effective_rationale,
    )


def make_request(*, policy=None, decision=None):
    return MetacognitiveInsufficientJustificationDetectionRequest.capture(
        policy=policy or make_detection_policy(),
        decision=decision or make_decision(),
        requested_by="ELMAN_SUPERVISOR",
        requested_at=REQUESTED_AT,
        reason="Audit deterministic justification sufficiency.",
    )


class PolicyTests(unittest.TestCase):
    def test_policy_round_trip_and_hash_are_deterministic(self):
        policy = make_detection_policy()
        restored = type(policy).from_json(policy.to_json())
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_policy_is_immutable(self):
        policy = make_detection_policy()
        with self.assertRaises(FrozenInstanceError):
            policy.minimum_rationale_characters = 1

    def test_policy_rejects_disabled_fail_closed(self):
        with self.assertRaises(
            MetacognitiveInsufficientJustificationDetectionPolicyError
        ):
            make_detection_policy(fail_closed=False)

    def test_policy_rejects_approval_citation_without_reference_enforcement(self):
        with self.assertRaises(
            MetacognitiveInsufficientJustificationDetectionPolicyError
        ):
            make_detection_policy(
                require_approval_reference_when_required=False,
                require_approval_reference_citation=True,
            )


class RequestTests(unittest.TestCase):
    def test_request_round_trip_is_hash_bound(self):
        request = make_request()
        restored = type(request).from_json(request.to_json())
        self.assertEqual(restored, request)
        restored.verify_hash()

    def test_request_rejects_tampered_decision_hash(self):
        request = make_request()
        data = json.loads(request.to_json())
        data["decision_hash"] = digest("f")
        data["request_hash"] = request.request_hash
        with self.assertRaises(
            MetacognitiveInsufficientJustificationDetectionIntegrityError
        ):
            type(request).from_dict(data)


class DetectorTests(unittest.TestCase):
    def test_sufficient_continue_decision_passes(self):
        result = detect_insufficient_justification(make_request())
        self.assertIs(
            result.status,
            MetacognitiveInsufficientJustificationDetectionStatus.SUFFICIENT,
        )
        self.assertEqual(result.gaps, ())
        self.assertEqual(result.findings, ())

    def test_short_rationale_is_detected(self):
        policy = make_detection_policy(
            minimum_cited_evidence_references=0,
            require_all_finding_citations=False,
            require_corrective_step_citations=False,
            require_approval_reference_citation=False,
        )
        decision = make_decision(rationale="Too short.")
        result = detect_insufficient_justification(
            make_request(policy=policy, decision=decision)
        )
        self.assertEqual(len(result.gaps), 1)
        self.assertIs(
            result.gaps[0].kind,
            MetacognitiveJustificationGapKind.RATIONALE_TOO_SHORT,
        )

    def test_missing_context_evidence_citation_is_detected(self):
        decision = make_decision(
            rationale=(
                "This rationale is deliberately long enough but contains "
                "no exact context evidence reference for the audit."
            )
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveJustificationGapKind
            .INSUFFICIENT_EVIDENCE_CITATIONS,
            kinds,
        )

    def test_missing_finding_citation_is_detected(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
            approval_reference="approval:pause-001",
            rationale=(
                "Pause is supported by evidence:journal-001 and "
                "approval:pause-001, but the finding identifier is omitted."
            ),
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveJustificationGapKind.MISSING_FINDING_CITATION,
            kinds,
        )

    def test_finding_hash_citation_satisfies_finding_requirement(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
            approval_reference="approval:pause-001",
            rationale=(
                "Pause is supported by evidence:journal-001, finding "
                f"{finding.finding_hash}, and approval:pause-001."
            ),
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertNotIn(
            MetacognitiveJustificationGapKind.MISSING_FINDING_CITATION,
            kinds,
        )

    def test_missing_corrective_step_citation_is_detected(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.CORRECT,
            corrective_steps=("step.1",),
            rationale=(
                "Correction is supported by evidence:journal-001 and finding "
                f"{finding.finding_hash}, while the corrective step token "
                "is deliberately omitted."
            ),
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveJustificationGapKind
            .MISSING_CORRECTIVE_STEP_CITATION,
            kinds,
        )

    def test_missing_required_approval_reference_is_detected(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
            approval_reference=None,
            rationale=(
                "Pause is supported by evidence:journal-001 and finding "
                f"{finding.finding_hash}; approval remains required."
            ),
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveJustificationGapKind.MISSING_APPROVAL_REFERENCE,
            kinds,
        )

    def test_missing_approval_reference_citation_is_detected(self):
        context = make_context()
        finding = make_finding(context)
        decision = make_decision(
            context=context,
            findings=(finding,),
            action=MetacognitiveDecisionAction.PAUSE,
            approval_required=True,
            approval_reference="approval:pause-001",
            rationale=(
                "Pause is supported by evidence:journal-001 and finding "
                f"{finding.finding_hash}, but the approval token is omitted."
            ),
        )
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveJustificationGapKind
            .MISSING_APPROVAL_REFERENCE_CITATION,
            kinds,
        )

    def test_detector_is_deterministic(self):
        request = make_request()
        first = detect_insufficient_justification(request)
        second = detect_insufficient_justification(request)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.result_hash, second.result_hash)

    def test_detector_does_not_mutate_decision(self):
        decision = make_decision()
        before = decision.to_json()
        detect_insufficient_justification(make_request(decision=decision))
        self.assertEqual(decision.to_json(), before)

    def test_result_round_trip_preserves_integrity(self):
        decision = make_decision(rationale="Too short.")
        result = detect_insufficient_justification(
            make_request(decision=decision)
        )
        restored = MetacognitiveInsufficientJustificationDetectionResult.from_json(
            result.to_json()
        )
        self.assertEqual(restored, result)
        restored.verify_hash()


if __name__ == "__main__":
    unittest.main()
