from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from elman_os.metacognitive_confidence_report import (
    METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION,
    MetacognitiveConfidenceLevel,
    MetacognitiveConfidenceReport,
    MetacognitiveConfidenceReportError,
    MetacognitiveConfidenceReportIntegrityError,
    MetacognitiveConfidenceReportPolicy,
    MetacognitiveConfidenceReportPolicyError,
    MetacognitiveConfidenceReportRequest,
    MetacognitiveConfidenceReporter,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionFinding,
)


BASE = datetime(2026, 8, 7, 1, 30, tzinfo=UTC)


class ConfidenceReportFixtures:
    @staticmethod
    def context(
        *,
        evidence: tuple[str, ...] = (
            "evidence:journal",
            "evidence:tests",
        ),
        suffix: str = "001",
    ) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.capture(
            plan_id=f"plan-confidence-{suffix}",
            project_id=f"project-confidence-{suffix}",
            plan_state_hash="1" * 64,
            journal_hash="2" * 64,
            checkpoint_hash="3" * 64,
            evidence_references=evidence,
            observed_by="SUPERVISOR_AGENT",
            observed_at=BASE,
            objective="Produce a deterministic structural confidence report.",
        )

    @staticmethod
    def policy(**overrides: object) -> MetacognitiveConfidenceReportPolicy:
        values = {
            "policy_id": "confidence-report-policy-v1",
            "low_confidence_threshold_bp": 4000,
            "medium_confidence_threshold_bp": 6000,
            "high_confidence_threshold_bp": 8000,
            "uncertainty_cap_bp": 5999,
            "evidence_gap_cap_bp": 4999,
            "minimum_findings": 1,
            "fail_closed": True,
        }
        values.update(overrides)
        return MetacognitiveConfidenceReportPolicy(**values)

    @staticmethod
    def finding(
        context: MetacognitiveSupervisionContext,
        *,
        kind: MetacognitiveFindingKind = MetacognitiveFindingKind.OTHER,
        confidence_bp: int = 9000,
        evidence: tuple[str, ...] = ("evidence:journal",),
        summary: str = "Deterministic observation.",
    ) -> MetacognitiveSupervisionFinding:
        return MetacognitiveSupervisionFinding.from_context(
            context=context,
            kind=kind,
            risk_level=MetacognitiveRiskLevel.MEDIUM,
            summary=summary,
            evidence_references=evidence,
            affected_step_ids=("step-a",),
            confidence_bp=confidence_bp,
        )

    @classmethod
    def request(
        cls,
        *,
        context: MetacognitiveSupervisionContext | None = None,
        findings: tuple[MetacognitiveSupervisionFinding, ...] | None = None,
        policy: MetacognitiveConfidenceReportPolicy | None = None,
    ) -> MetacognitiveConfidenceReportRequest:
        actual_context = context or cls.context()
        actual_findings = findings
        if actual_findings is None:
            actual_findings = (
                cls.finding(
                    actual_context,
                    confidence_bp=9200,
                    evidence=("evidence:journal",),
                    summary="Journal evidence is internally consistent.",
                ),
                cls.finding(
                    actual_context,
                    confidence_bp=8500,
                    evidence=("evidence:tests",),
                    summary="Test evidence is bound to the same context.",
                ),
            )
        return MetacognitiveConfidenceReportRequest.capture(
            policy=policy or cls.policy(),
            context=actual_context,
            findings=actual_findings,
            requested_by="SUPERVISOR_AGENT",
            requested_at=BASE + timedelta(minutes=1),
            reason="Summarize confidence in bound metacognitive evidence.",
        )


class TestMetacognitiveConfidenceReportPolicy(unittest.TestCase):
    def test_default_policy_is_fail_closed(self) -> None:
        policy = ConfidenceReportFixtures.policy()
        self.assertTrue(policy.fail_closed)
        self.assertEqual(
            policy.version,
            METACOGNITIVE_CONFIDENCE_REPORT_FORMAT_VERSION,
        )

    def test_policy_round_trip_is_canonical(self) -> None:
        policy = ConfidenceReportFixtures.policy()
        restored = MetacognitiveConfidenceReportPolicy.from_json(
            policy.to_json()
        )
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_policy_hash_is_deterministic(self) -> None:
        first = ConfidenceReportFixtures.policy()
        second = ConfidenceReportFixtures.policy()
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_policy_rejects_non_fail_closed_mode(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportPolicyError):
            ConfidenceReportFixtures.policy(fail_closed=False)

    def test_policy_rejects_non_increasing_thresholds(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportPolicyError):
            ConfidenceReportFixtures.policy(
                low_confidence_threshold_bp=6000,
                medium_confidence_threshold_bp=6000,
            )

    def test_policy_rejects_evidence_gap_cap_above_uncertainty_cap(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportPolicyError):
            ConfidenceReportFixtures.policy(
                uncertainty_cap_bp=5000,
                evidence_gap_cap_bp=5001,
            )

    def test_policy_rejects_boolean_as_basis_points(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportError):
            ConfidenceReportFixtures.policy(
                high_confidence_threshold_bp=True
            )

    def test_policy_rejects_zero_minimum_findings(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportError):
            ConfidenceReportFixtures.policy(minimum_findings=0)


class TestMetacognitiveConfidenceReportRequest(unittest.TestCase):
    def test_request_round_trip_is_canonical(self) -> None:
        request = ConfidenceReportFixtures.request()
        restored = MetacognitiveConfidenceReportRequest.from_json(
            request.to_json()
        )
        self.assertEqual(restored, request)
        self.assertEqual(restored.request_hash, request.request_hash)

    def test_request_hash_is_deterministic(self) -> None:
        first = ConfidenceReportFixtures.request()
        second = ConfidenceReportFixtures.request()
        self.assertEqual(first.request_hash, second.request_hash)
        self.assertEqual(first.request_id, second.request_id)

    def test_request_sorts_findings_deterministically(self) -> None:
        context = ConfidenceReportFixtures.context()
        first = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=8200,
            evidence=("evidence:journal",),
            summary="First observation.",
        )
        second = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=9100,
            evidence=("evidence:tests",),
            summary="Second observation.",
        )
        left = ConfidenceReportFixtures.request(
            context=context,
            findings=(first, second),
        )
        right = ConfidenceReportFixtures.request(
            context=context,
            findings=(second, first),
        )
        self.assertEqual(left.request_hash, right.request_hash)
        self.assertEqual(left.finding_hashes, right.finding_hashes)

    def test_request_rejects_duplicate_findings(self) -> None:
        context = ConfidenceReportFixtures.context()
        finding = ConfidenceReportFixtures.finding(context)
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            ConfidenceReportFixtures.request(
                context=context,
                findings=(finding, finding),
            )

    def test_request_rejects_finding_from_other_context(self) -> None:
        context = ConfidenceReportFixtures.context(suffix="001")
        other = ConfidenceReportFixtures.context(suffix="002")
        finding = ConfidenceReportFixtures.finding(other)
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            ConfidenceReportFixtures.request(
                context=context,
                findings=(finding,),
            )

    def test_request_rejects_timestamp_before_context(self) -> None:
        context = ConfidenceReportFixtures.context()
        with self.assertRaises(MetacognitiveConfidenceReportPolicyError):
            MetacognitiveConfidenceReportRequest.capture(
                policy=ConfidenceReportFixtures.policy(),
                context=context,
                findings=(),
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE - timedelta(seconds=1),
                reason="Invalid chronology.",
            )

    def test_request_rejects_tampered_policy_hash(self) -> None:
        request = ConfidenceReportFixtures.request()
        data = request.to_dict()
        data["policy_hash"] = "0" * 64
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReportRequest.from_dict(data)

    def test_request_rejects_tampered_context_hash(self) -> None:
        request = ConfidenceReportFixtures.request()
        data = request.to_dict()
        data["context_hash"] = "0" * 64
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReportRequest.from_dict(data)

    def test_request_rejects_tampered_finding_hashes(self) -> None:
        request = ConfidenceReportFixtures.request()
        data = request.to_dict()
        data["finding_hashes"] = ["0" * 64]
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReportRequest.from_dict(data)

    def test_request_rejects_missing_request_hash(self) -> None:
        request = ConfidenceReportFixtures.request()
        data = request.to_dict()
        del data["request_hash"]
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReportRequest.from_dict(data)

    def test_request_rejects_tampered_request_hash(self) -> None:
        request = ConfidenceReportFixtures.request()
        data = request.to_dict()
        data["request_hash"] = "0" * 64
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReportRequest.from_dict(data)


class TestMetacognitiveConfidenceReporter(unittest.TestCase):
    def test_full_coverage_uses_lowest_finding_confidence(self) -> None:
        request = ConfidenceReportFixtures.request()
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.finding_count, 2)
        self.assertEqual(report.context_evidence_count, 2)
        self.assertEqual(report.evidence_coverage_bp, 10_000)
        self.assertEqual(report.finding_confidence_floor_bp, 8500)
        self.assertEqual(report.applicable_cap_bp, 10_000)
        self.assertEqual(report.overall_confidence_bp, 8500)
        self.assertEqual(report.confidence_level, MetacognitiveConfidenceLevel.HIGH)

    def test_partial_evidence_coverage_limits_confidence(self) -> None:
        context = ConfidenceReportFixtures.context()
        finding = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=9500,
            evidence=("evidence:journal",),
        )
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(finding,),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.evidence_coverage_bp, 5000)
        self.assertEqual(report.overall_confidence_bp, 5000)
        self.assertEqual(report.confidence_level, MetacognitiveConfidenceLevel.LOW)

    def test_finding_confidence_floor_limits_confidence(self) -> None:
        context = ConfidenceReportFixtures.context(
            evidence=("evidence:journal",)
        )
        strong = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=9500,
            evidence=("evidence:journal",),
            summary="Strong observation.",
        )
        weak = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=6100,
            evidence=("evidence:journal",),
            summary="Weaker observation.",
        )
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(strong, weak),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.overall_confidence_bp, 6100)
        self.assertEqual(
            report.confidence_level,
            MetacognitiveConfidenceLevel.MEDIUM,
        )

    def test_uncertainty_finding_applies_cap(self) -> None:
        context = ConfidenceReportFixtures.context(
            evidence=("evidence:journal",)
        )
        finding = ConfidenceReportFixtures.finding(
            context,
            kind=MetacognitiveFindingKind.UNCERTAINTY,
            confidence_bp=9500,
            evidence=("evidence:journal",),
        )
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(finding,),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.applicable_cap_bp, 5999)
        self.assertEqual(report.overall_confidence_bp, 5999)
        self.assertEqual(report.confidence_level, MetacognitiveConfidenceLevel.LOW)

    def test_evidence_gap_finding_applies_stricter_cap(self) -> None:
        context = ConfidenceReportFixtures.context(
            evidence=("evidence:journal",)
        )
        finding = ConfidenceReportFixtures.finding(
            context,
            kind=MetacognitiveFindingKind.EVIDENCE_GAP,
            confidence_bp=9500,
            evidence=("evidence:journal",),
        )
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(finding,),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.applicable_cap_bp, 4999)
        self.assertEqual(report.overall_confidence_bp, 4999)
        self.assertEqual(report.confidence_level, MetacognitiveConfidenceLevel.LOW)

    def test_evidence_gap_wins_over_uncertainty_cap(self) -> None:
        context = ConfidenceReportFixtures.context(
            evidence=("evidence:journal", "evidence:tests")
        )
        uncertainty = ConfidenceReportFixtures.finding(
            context,
            kind=MetacognitiveFindingKind.UNCERTAINTY,
            confidence_bp=9000,
            evidence=("evidence:journal",),
            summary="Uncertainty remains.",
        )
        gap = ConfidenceReportFixtures.finding(
            context,
            kind=MetacognitiveFindingKind.EVIDENCE_GAP,
            confidence_bp=9000,
            evidence=("evidence:tests",),
            summary="Evidence gap remains.",
        )
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(uncertainty, gap),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.applicable_cap_bp, 4999)

    def test_no_findings_is_fail_closed(self) -> None:
        context = ConfidenceReportFixtures.context()
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(),
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.finding_count, 0)
        self.assertEqual(report.evidence_coverage_bp, 0)
        self.assertEqual(report.overall_confidence_bp, 0)
        self.assertEqual(
            report.confidence_level,
            MetacognitiveConfidenceLevel.INSUFFICIENT,
        )

    def test_minimum_findings_is_enforced_fail_closed(self) -> None:
        context = ConfidenceReportFixtures.context(
            evidence=("evidence:journal",)
        )
        finding = ConfidenceReportFixtures.finding(
            context,
            confidence_bp=9500,
            evidence=("evidence:journal",),
        )
        policy = ConfidenceReportFixtures.policy(minimum_findings=2)
        request = ConfidenceReportFixtures.request(
            context=context,
            findings=(finding,),
            policy=policy,
        )
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.overall_confidence_bp, 0)
        self.assertEqual(
            report.confidence_level,
            MetacognitiveConfidenceLevel.INSUFFICIENT,
        )

    def test_report_is_deterministic_for_same_request(self) -> None:
        request = ConfidenceReportFixtures.request()
        reporter = MetacognitiveConfidenceReporter()
        first = reporter.generate(request)
        second = reporter.generate(request)
        self.assertEqual(first, second)
        self.assertEqual(first.report_hash, second.report_hash)
        self.assertEqual(first.report_id, second.report_id)

    def test_report_round_trip_is_canonical(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        restored = MetacognitiveConfidenceReport.from_json(report.to_json())
        self.assertEqual(restored, report)
        self.assertEqual(restored.report_hash, report.report_hash)

    def test_report_is_bound_to_request_author_and_time(self) -> None:
        request = ConfidenceReportFixtures.request()
        report = MetacognitiveConfidenceReporter().generate(request)
        self.assertEqual(report.generated_by, request.requested_by)
        self.assertEqual(report.generated_at, request.requested_at)

    def test_report_rationale_is_structural_not_authoritative(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        self.assertIn("Structural confidence", report.rationale)
        self.assertNotIn("authorize", report.rationale.lower())

    def test_report_rejects_tampered_score(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["overall_confidence_bp"] -= 1
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_tampered_level(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["confidence_level"] = "low"
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_tampered_coverage(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["evidence_coverage_bp"] = 9999
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_tampered_covered_evidence(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["covered_evidence_references"] = ["evidence:journal"]
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_tampered_request_hash(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["request_hash"] = "0" * 64
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_tampered_report_hash(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        data["report_hash"] = "0" * 64
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_missing_report_hash(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        data = report.to_dict()
        del data["report_hash"]
        with self.assertRaises(MetacognitiveConfidenceReportIntegrityError):
            MetacognitiveConfidenceReport.from_dict(data)

    def test_report_rejects_non_request_input(self) -> None:
        with self.assertRaises(MetacognitiveConfidenceReportError):
            MetacognitiveConfidenceReporter().generate(object())  # type: ignore[arg-type]

    def test_report_json_is_valid_object(self) -> None:
        report = MetacognitiveConfidenceReporter().generate(
            ConfidenceReportFixtures.request()
        )
        parsed = json.loads(report.to_json())
        self.assertIsInstance(parsed, dict)
        self.assertEqual(
            parsed["record_type"],
            "metacognitive_confidence_report",
        )


if __name__ == "__main__":
    unittest.main()
