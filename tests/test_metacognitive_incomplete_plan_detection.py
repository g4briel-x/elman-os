from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from elman_os.execution_plan import ExecutionPlan, ExecutionStep
from elman_os.metacognitive_incomplete_plan_detection import (
    METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION,
    MetacognitiveIncompletePlanDetectionError,
    MetacognitiveIncompletePlanDetectionIntegrityError,
    MetacognitiveIncompletePlanDetectionPolicy,
    MetacognitiveIncompletePlanDetectionPolicyError,
    MetacognitiveIncompletePlanDetectionRequest,
    MetacognitiveIncompletePlanDetectionResult,
    MetacognitiveIncompletePlanDetectionStatus,
    MetacognitiveIncompletePlanDetector,
    MetacognitiveIncompletePlanGapKind,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
)

BASE = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
EVIDENCE = "plan:plan-completeness-001"


def plan_state_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()


class Fixtures:
    @staticmethod
    def step(
        step_id: str,
        capability_id: str,
        *,
        dependencies: tuple[str, ...] = (),
        assigned_agent_id: str | None = None,
        requires_human_approval: bool = False,
        approval_reference: str | None = None,
    ) -> ExecutionStep:
        return ExecutionStep(
            step_id=step_id,
            title=f"Title {step_id}",
            capability_id=capability_id,
            objective=f"Objective {step_id}",
            dependencies=dependencies,
            assigned_agent_id=assigned_agent_id,
            requires_human_approval=requires_human_approval,
            approval_reference=approval_reference,
        )

    @classmethod
    def plan(
        cls,
        *,
        include_verify: bool = True,
        bound: bool = True,
        requires_human_approval: bool = False,
        plan_approval_reference: str | None = None,
        verify_requires_approval: bool = False,
        verify_approval_reference: str | None = None,
    ) -> ExecutionPlan:
        steps = [
            cls.step(
                "step.build",
                "build.app",
                assigned_agent_id=("BUILD_AGENT" if bound else None),
            )
        ]
        if include_verify:
            steps.append(
                cls.step(
                    "step.verify",
                    "verify.app",
                    dependencies=("step.build",),
                    assigned_agent_id=("VERIFY_AGENT" if bound else None),
                    requires_human_approval=verify_requires_approval,
                    approval_reference=verify_approval_reference,
                )
            )
        return ExecutionPlan(
            plan_id="plan-completeness-001",
            project_id="project-completeness-001",
            objective="Build and verify the requested application.",
            created_by="ORCHESTRATOR_AGENT",
            steps=tuple(steps),
            requires_human_approval=requires_human_approval,
            approval_reference=plan_approval_reference,
        )

    @staticmethod
    def context(plan: ExecutionPlan) -> MetacognitiveSupervisionContext:
        return MetacognitiveSupervisionContext.capture(
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            plan_state_hash=plan_state_hash(plan),
            journal_hash="1" * 64,
            checkpoint_hash="2" * 64,
            evidence_references=(EVIDENCE,),
            observed_by="SUPERVISOR_AGENT",
            observed_at=BASE + timedelta(minutes=1),
            objective="Assess declared execution-plan completeness.",
        )

    @staticmethod
    def policy(**overrides: object) -> MetacognitiveIncompletePlanDetectionPolicy:
        values = {
            "policy_id": "incomplete-plan-policy-v1",
            "required_step_ids": ("step.build", "step.verify"),
            "required_capability_ids": ("build.app", "verify.app"),
            "minimum_step_count": 2,
            "require_bound_agents": False,
            "require_approval_trace": False,
            "finding_confidence_bp": 9500,
            "fail_closed": True,
        }
        values.update(overrides)
        return MetacognitiveIncompletePlanDetectionPolicy(**values)

    @classmethod
    def request(
        cls,
        plan: ExecutionPlan,
        *,
        policy: MetacognitiveIncompletePlanDetectionPolicy | None = None,
    ) -> MetacognitiveIncompletePlanDetectionRequest:
        return MetacognitiveIncompletePlanDetectionRequest.capture(
            policy=policy or cls.policy(),
            context=cls.context(plan),
            plan=plan,
            plan_evidence_reference=EVIDENCE,
            requested_by="SUPERVISOR_AGENT",
            requested_at=BASE + timedelta(minutes=2),
            reason="Check explicit completeness requirements.",
        )

    @classmethod
    def result(
        cls,
        plan: ExecutionPlan,
        *,
        policy: MetacognitiveIncompletePlanDetectionPolicy | None = None,
    ) -> MetacognitiveIncompletePlanDetectionResult:
        return MetacognitiveIncompletePlanDetector().detect(
            request=cls.request(plan, policy=policy),
            completed_at=BASE + timedelta(minutes=3),
        )


class TestPolicy(unittest.TestCase):
    def test_policy_round_trip_and_hash(self) -> None:
        policy = Fixtures.policy()
        restored = MetacognitiveIncompletePlanDetectionPolicy.from_json(
            policy.to_json()
        )
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)
        self.assertTrue(policy.fail_closed)
        self.assertEqual(
            policy.version,
            METACOGNITIVE_INCOMPLETE_PLAN_DETECTION_FORMAT_VERSION,
        )

    def test_policy_normalizes_required_coverage(self) -> None:
        policy = Fixtures.policy(
            required_step_ids=("step.verify", "step.build", "step.verify"),
            required_capability_ids=("verify.app", "build.app", "verify.app"),
        )
        self.assertEqual(policy.required_step_ids, ("step.build", "step.verify"))
        self.assertEqual(
            policy.required_capability_ids,
            ("build.app", "verify.app"),
        )

    def test_policy_rejects_non_fail_closed_mode(self) -> None:
        with self.assertRaises(MetacognitiveIncompletePlanDetectionPolicyError):
            Fixtures.policy(fail_closed=False)

    def test_policy_rejects_invalid_minimum(self) -> None:
        with self.assertRaises(MetacognitiveIncompletePlanDetectionError):
            Fixtures.policy(minimum_step_count=0)


class TestRequest(unittest.TestCase):
    def test_request_binds_plan_state(self) -> None:
        plan = Fixtures.plan()
        request = Fixtures.request(plan)
        self.assertEqual(request.plan_state_hash, plan_state_hash(plan))
        self.assertEqual(
            request.plan_state_hash,
            request.context.plan_state_hash,
        )

    def test_request_round_trip(self) -> None:
        request = Fixtures.request(Fixtures.plan())
        restored = MetacognitiveIncompletePlanDetectionRequest.from_json(
            request.to_json()
        )
        restored.verify_hash()
        self.assertEqual(restored, request)

    def test_request_rejects_unbound_evidence(self) -> None:
        plan = Fixtures.plan()
        with self.assertRaises(
            MetacognitiveIncompletePlanDetectionIntegrityError
        ):
            MetacognitiveIncompletePlanDetectionRequest.capture(
                policy=Fixtures.policy(),
                context=Fixtures.context(plan),
                plan=plan,
                plan_evidence_reference="plan:other",
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE + timedelta(minutes=2),
                reason="Invalid evidence binding.",
            )

    def test_request_rejects_other_plan_state(self) -> None:
        plan = Fixtures.plan()
        other = Fixtures.plan(include_verify=False)
        with self.assertRaises(
            MetacognitiveIncompletePlanDetectionIntegrityError
        ):
            MetacognitiveIncompletePlanDetectionRequest.capture(
                policy=Fixtures.policy(),
                context=Fixtures.context(other),
                plan=plan,
                plan_evidence_reference=EVIDENCE,
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE + timedelta(minutes=2),
                reason="Reject stale context.",
            )

    def test_request_rejects_time_before_context(self) -> None:
        plan = Fixtures.plan()
        with self.assertRaises(
            MetacognitiveIncompletePlanDetectionPolicyError
        ):
            MetacognitiveIncompletePlanDetectionRequest.capture(
                policy=Fixtures.policy(),
                context=Fixtures.context(plan),
                plan=plan,
                plan_evidence_reference=EVIDENCE,
                requested_by="SUPERVISOR_AGENT",
                requested_at=BASE,
                reason="Invalid request time.",
            )


class TestDetection(unittest.TestCase):
    def test_complete_plan_is_clear(self) -> None:
        result = Fixtures.result(Fixtures.plan())
        self.assertIs(
            result.status,
            MetacognitiveIncompletePlanDetectionStatus.COMPLETE,
        )
        self.assertEqual(result.gaps, ())
        self.assertEqual(result.findings, ())

    def test_missing_step_capability_and_minimum_are_detected(self) -> None:
        result = Fixtures.result(Fixtures.plan(include_verify=False))
        kinds = {gap.kind for gap in result.gaps}
        self.assertIn(
            MetacognitiveIncompletePlanGapKind.MISSING_REQUIRED_STEP,
            kinds,
        )
        self.assertIn(
            MetacognitiveIncompletePlanGapKind.MISSING_REQUIRED_CAPABILITY,
            kinds,
        )
        self.assertIn(
            MetacognitiveIncompletePlanGapKind.MINIMUM_STEP_COUNT,
            kinds,
        )

    def test_missing_capability_is_detected(self) -> None:
        plan = ExecutionPlan(
            plan_id="plan-completeness-001",
            project_id="project-completeness-001",
            objective="Two steps without verification capability.",
            created_by="ORCHESTRATOR_AGENT",
            steps=(
                Fixtures.step(
                    "step.build",
                    "build.app",
                    assigned_agent_id="BUILD_AGENT",
                ),
                Fixtures.step(
                    "step.verify",
                    "build.app",
                    dependencies=("step.build",),
                    assigned_agent_id="VERIFY_AGENT",
                ),
            ),
        )
        result = Fixtures.result(plan)
        self.assertIn(
            MetacognitiveIncompletePlanGapKind.MISSING_REQUIRED_CAPABILITY,
            {gap.kind for gap in result.gaps},
        )

    def test_unbound_steps_are_policy_controlled(self) -> None:
        plan = Fixtures.plan(bound=False)
        strict = Fixtures.result(
            plan,
            policy=Fixtures.policy(require_bound_agents=True),
        )
        relaxed = Fixtures.result(
            plan,
            policy=Fixtures.policy(require_bound_agents=False),
        )
        self.assertEqual(
            sum(
                gap.kind is MetacognitiveIncompletePlanGapKind.UNBOUND_STEP
                for gap in strict.gaps
            ),
            2,
        )
        self.assertIs(
            relaxed.status,
            MetacognitiveIncompletePlanDetectionStatus.COMPLETE,
        )

    def test_missing_plan_approval_is_detected(self) -> None:
        result = Fixtures.result(
            Fixtures.plan(requires_human_approval=True),
            policy=Fixtures.policy(require_approval_trace=True),
        )
        self.assertIn(
            MetacognitiveIncompletePlanGapKind.MISSING_PLAN_APPROVAL,
            {gap.kind for gap in result.gaps},
        )

    def test_plan_approval_satisfies_step_trace(self) -> None:
        result = Fixtures.result(
            Fixtures.plan(
                requires_human_approval=True,
                plan_approval_reference="approval-plan-001",
                verify_requires_approval=True,
            ),
            policy=Fixtures.policy(require_approval_trace=True),
        )
        self.assertIs(
            result.status,
            MetacognitiveIncompletePlanDetectionStatus.COMPLETE,
        )

    def test_missing_step_approval_is_detected(self) -> None:
        result = Fixtures.result(
            Fixtures.plan(
                requires_human_approval=False,
                verify_requires_approval=True,
            ),
            policy=Fixtures.policy(require_approval_trace=True),
        )
        matching = [
            gap
            for gap in result.gaps
            if gap.kind
            is MetacognitiveIncompletePlanGapKind.MISSING_STEP_APPROVAL
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].affected_step_ids, ("step.verify",))

    def test_findings_are_context_bound_evidence_gaps(self) -> None:
        result = Fixtures.result(Fixtures.plan(include_verify=False))
        self.assertTrue(result.findings)
        for finding in result.findings:
            self.assertIs(finding.kind, MetacognitiveFindingKind.EVIDENCE_GAP)
            self.assertEqual(finding.context_hash, result.request.context_hash)
            self.assertEqual(finding.evidence_references, (EVIDENCE,))

    def test_unbound_gaps_are_medium_risk(self) -> None:
        result = Fixtures.result(
            Fixtures.plan(bound=False),
            policy=Fixtures.policy(require_bound_agents=True),
        )
        unbound = [
            gap
            for gap in result.gaps
            if gap.kind is MetacognitiveIncompletePlanGapKind.UNBOUND_STEP
        ]
        self.assertTrue(unbound)
        self.assertTrue(
            all(
                gap.risk_level is MetacognitiveRiskLevel.MEDIUM
                for gap in unbound
            )
        )

    def test_detector_is_read_only_and_deterministic(self) -> None:
        plan = Fixtures.plan(include_verify=False)
        before = plan.to_json()
        request = Fixtures.request(plan)
        detector = MetacognitiveIncompletePlanDetector()
        first = detector.detect(
            request=request,
            completed_at=BASE + timedelta(minutes=3),
        )
        second = detector.detect(
            request=request,
            completed_at=BASE + timedelta(minutes=3),
        )
        self.assertEqual(plan.to_json(), before)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.result_hash, second.result_hash)


class TestResult(unittest.TestCase):
    def test_result_round_trip(self) -> None:
        result = Fixtures.result(Fixtures.plan(include_verify=False))
        restored = MetacognitiveIncompletePlanDetectionResult.from_json(
            result.to_json()
        )
        restored.verify_hash()
        self.assertEqual(restored, result)

    def test_result_rejects_tampered_hash(self) -> None:
        result = Fixtures.result(Fixtures.plan(include_verify=False))
        data = result.to_dict()
        data["result_hash"] = "f" * 64
        with self.assertRaises(
            MetacognitiveIncompletePlanDetectionIntegrityError
        ):
            MetacognitiveIncompletePlanDetectionResult.from_dict(data)

    def test_completion_cannot_precede_request(self) -> None:
        request = Fixtures.request(Fixtures.plan())
        with self.assertRaises(
            MetacognitiveIncompletePlanDetectionPolicyError
        ):
            MetacognitiveIncompletePlanDetector().detect(
                request=request,
                completed_at=BASE,
            )


if __name__ == "__main__":
    unittest.main()
