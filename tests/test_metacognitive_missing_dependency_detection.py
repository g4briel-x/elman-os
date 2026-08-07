from __future__ import annotations

import hashlib
import json
import unittest

from elman_os.execution_plan import ExecutionPlan, ExecutionStep
from elman_os.metacognitive_missing_dependency_detection import (
    MetacognitiveDependencyRelation,
    MetacognitiveMissingDependencyDetectionIntegrityError,
    MetacognitiveMissingDependencyDetectionPolicy,
    MetacognitiveMissingDependencyDetectionPolicyError,
    MetacognitiveMissingDependencyDetectionRequest,
    MetacognitiveMissingDependencyDetectionResult,
    MetacognitiveMissingDependencyDetectionStatus,
    MetacognitiveRequiredDependency,
    detect_missing_dependencies,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveFindingKind,
    MetacognitiveSupervisionContext,
)


class MetacognitiveMissingDependencyDetectionTests(unittest.TestCase):
    def plan(
        self,
        *,
        deploy_dependencies: tuple[str, ...] = (),
        package_dependencies: tuple[str, ...] = ("step.test",),
    ) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-missing-dependency-001",
            project_id="project-missing-dependency-001",
            objective="Ship a validated release",
            created_by="ELMAN_SUPERVISOR",
            requires_human_approval=False,
            steps=(
                ExecutionStep(
                    step_id="step.test",
                    title="Test",
                    capability_id="testing",
                    objective="Validate the build",
                ),
                ExecutionStep(
                    step_id="step.package",
                    title="Package",
                    capability_id="packaging",
                    objective="Package the validated build",
                    dependencies=package_dependencies,
                ),
                ExecutionStep(
                    step_id="step.deploy",
                    title="Deploy",
                    capability_id="deployment",
                    objective="Deploy the release",
                    dependencies=deploy_dependencies,
                ),
            ),
        )

    def context(self, plan: ExecutionPlan) -> MetacognitiveSupervisionContext:
        plan_hash = hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()
        return MetacognitiveSupervisionContext.capture(
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            plan_state_hash=plan_hash,
            journal_hash="a" * 64,
            checkpoint_hash="b" * 64,
            evidence_references=("plan-evidence:001",),
            observed_by="ELMAN_SUPERVISOR",
            observed_at="2026-08-07T04:00:00.000000Z",
            objective=plan.objective,
        )

    def policy(
        self,
        relation: MetacognitiveDependencyRelation = MetacognitiveDependencyRelation.DIRECT,
        *,
        dependent: str = "step.deploy",
        prerequisite: str = "step.test",
    ) -> MetacognitiveMissingDependencyDetectionPolicy:
        return MetacognitiveMissingDependencyDetectionPolicy(
            policy_id="missing-dependency-policy:001",
            required_dependencies=(
                MetacognitiveRequiredDependency(
                    dependent_step_id=dependent,
                    prerequisite_step_id=prerequisite,
                    relation=relation,
                ),
            ),
        )

    def request(
        self,
        plan: ExecutionPlan,
        policy: MetacognitiveMissingDependencyDetectionPolicy,
    ) -> MetacognitiveMissingDependencyDetectionRequest:
        return MetacognitiveMissingDependencyDetectionRequest.capture(
            policy=policy,
            context=self.context(plan),
            plan=plan,
            plan_evidence_reference="plan-evidence:001",
            requested_by="ELMAN_SUPERVISOR",
            requested_at="2026-08-07T04:01:00.000000Z",
            reason="Verify explicit dependency completeness",
        )

    def test_direct_missing_dependency_emits_gap_and_finding(self) -> None:
        plan = self.plan(deploy_dependencies=())
        result = detect_missing_dependencies(self.request(plan, self.policy()))
        self.assertIs(result.status, MetacognitiveMissingDependencyDetectionStatus.MISSING)
        self.assertEqual(len(result.gaps), 1)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.gaps[0].dependent_step_id, "step.deploy")
        self.assertEqual(result.gaps[0].prerequisite_step_id, "step.test")
        self.assertEqual(result.findings[0].kind, MetacognitiveFindingKind.EVIDENCE_GAP)

    def test_direct_dependency_is_satisfied_when_declared(self) -> None:
        plan = self.plan(deploy_dependencies=("step.test",))
        result = detect_missing_dependencies(self.request(plan, self.policy()))
        self.assertIs(result.status, MetacognitiveMissingDependencyDetectionStatus.SATISFIED)
        self.assertEqual(result.gaps, ())
        self.assertEqual(result.findings, ())

    def test_transitive_dependency_is_satisfied_through_intermediate_step(self) -> None:
        plan = self.plan(deploy_dependencies=("step.package",))
        policy = self.policy(MetacognitiveDependencyRelation.TRANSITIVE)
        result = detect_missing_dependencies(self.request(plan, policy))
        self.assertIs(result.status, MetacognitiveMissingDependencyDetectionStatus.SATISFIED)

    def test_direct_requirement_is_not_satisfied_by_transitive_ancestry(self) -> None:
        plan = self.plan(deploy_dependencies=("step.package",))
        result = detect_missing_dependencies(self.request(plan, self.policy()))
        self.assertIs(result.status, MetacognitiveMissingDependencyDetectionStatus.MISSING)
        self.assertEqual(result.gaps[0].relation, MetacognitiveDependencyRelation.DIRECT)

    def test_transitive_requirement_missing_when_no_ancestry_exists(self) -> None:
        plan = self.plan(deploy_dependencies=())
        policy = self.policy(MetacognitiveDependencyRelation.TRANSITIVE)
        result = detect_missing_dependencies(self.request(plan, policy))
        self.assertIs(result.status, MetacognitiveMissingDependencyDetectionStatus.MISSING)

    def test_policy_referencing_absent_step_fails_closed(self) -> None:
        plan = self.plan()
        policy = self.policy(dependent="step.unknown")
        with self.assertRaises(MetacognitiveMissingDependencyDetectionPolicyError):
            detect_missing_dependencies(self.request(plan, policy))

    def test_policy_round_trip_is_hash_stable(self) -> None:
        policy = self.policy(MetacognitiveDependencyRelation.TRANSITIVE)
        restored = MetacognitiveMissingDependencyDetectionPolicy.from_json(policy.to_json())
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_request_round_trip_is_hash_stable(self) -> None:
        plan = self.plan()
        request = self.request(plan, self.policy())
        restored = MetacognitiveMissingDependencyDetectionRequest.from_json(request.to_json())
        self.assertEqual(restored.request_hash, request.request_hash)
        self.assertEqual(restored.request_id, request.request_id)

    def test_result_round_trip_is_hash_stable(self) -> None:
        plan = self.plan()
        result = detect_missing_dependencies(self.request(plan, self.policy()))
        restored = MetacognitiveMissingDependencyDetectionResult.from_json(result.to_json())
        self.assertEqual(restored.result_hash, result.result_hash)
        self.assertEqual(restored.result_id, result.result_id)

    def test_request_tampering_is_detected(self) -> None:
        plan = self.plan()
        request = self.request(plan, self.policy())
        data = json.loads(request.to_json())
        data["reason"] = "Tampered reason"
        with self.assertRaises(MetacognitiveMissingDependencyDetectionIntegrityError):
            MetacognitiveMissingDependencyDetectionRequest.from_dict(data)

    def test_detector_does_not_mutate_plan(self) -> None:
        plan = self.plan(deploy_dependencies=("step.package",))
        before = plan.to_json()
        detect_missing_dependencies(
            self.request(plan, self.policy(MetacognitiveDependencyRelation.TRANSITIVE))
        )
        self.assertEqual(plan.to_json(), before)

    def test_duplicate_policy_requirements_are_rejected(self) -> None:
        requirement = MetacognitiveRequiredDependency(
            dependent_step_id="step.deploy",
            prerequisite_step_id="step.test",
        )
        with self.assertRaises(MetacognitiveMissingDependencyDetectionPolicyError):
            MetacognitiveMissingDependencyDetectionPolicy(
                policy_id="missing-dependency-policy:duplicates",
                required_dependencies=(requirement, requirement),
            )


if __name__ == "__main__":
    unittest.main()
