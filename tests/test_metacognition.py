import unittest

from elman_os.domain import CycleResult, ReflectionReport, StopReason, Verdict
from elman_os.metacognition import (
    LearningAgent,
    MemoryManager,
    MetacognitiveSupervisor,
    ReflectiveAgent,
    SupervisorPolicy,
)


class SupervisorTests(unittest.TestCase):
    def test_success_stops_for_human_approval(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.PASS,
                criteria_validated=True,
                progress_score=1.0,
                cost_units=1.0,
                evidence=["test passé"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertFalse(decision.should_continue)
        self.assertEqual(decision.reason, StopReason.CRITERIA_VALIDATED)
        self.assertTrue(decision.requires_human_decision)

    def test_pass_with_warnings_stops_for_human_approval(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.PASS_WITH_WARNINGS,
                criteria_validated=True,
                progress_score=1.0,
                cost_units=1.0,
                evidence=["test passé", "risque mineur documenté"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.CRITERIA_VALIDATED)
        self.assertIn("avertissements", decision.message)

    def test_repeated_failure_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(max_iterations=5, max_same_failure=2)
        )
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.2,
            cost_units=1.0,
            failure_fingerprint="same",
        )
        first = supervisor.evaluate(
            iteration=1,
            result=result,
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        second = supervisor.evaluate(
            iteration=2,
            result=result,
            cumulative_cost=2.0,
            elapsed_seconds=2.0,
        )
        self.assertTrue(first.should_continue)
        self.assertEqual(second.reason, StopReason.REPEATED_FAILURE)

    def test_critical_finding_stops_immediately(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.BLOCKED,
                criteria_validated=False,
                progress_score=0.1,
                cost_units=1.0,
                critical_findings=["secret exposé"],
            ),
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.CRITICAL_FINDING)

    def test_max_iterations_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(
                max_iterations=2,
                max_same_failure=10,
                max_no_progress=10,
            )
        )
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.2,
            cost_units=1.0,
        )
        supervisor.evaluate(
            iteration=1,
            result=result,
            cumulative_cost=1.0,
            elapsed_seconds=1.0,
        )
        decision = supervisor.evaluate(
            iteration=2,
            result=result,
            cumulative_cost=2.0,
            elapsed_seconds=2.0,
        )
        self.assertEqual(decision.reason, StopReason.MAX_ITERATIONS)

    def test_no_progress_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(
                max_iterations=6,
                max_same_failure=10,
                max_no_progress=2,
                minimum_progress_delta=0.05,
            )
        )
        for iteration in (1, 2):
            decision = supervisor.evaluate(
                iteration=iteration,
                result=CycleResult(
                    proof_verdict=Verdict.REWORK_REQUIRED,
                    criteria_validated=False,
                    progress_score=0.5,
                    cost_units=1.0,
                    failure_fingerprint=f"failure-{iteration}",
                ),
                cumulative_cost=float(iteration),
                elapsed_seconds=float(iteration),
            )
        self.assertTrue(decision.should_continue)
        decision = supervisor.evaluate(
            iteration=3,
            result=CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.5,
                cost_units=1.0,
                failure_fingerprint="failure-3",
            ),
            cumulative_cost=3.0,
            elapsed_seconds=3.0,
        )
        self.assertEqual(decision.reason, StopReason.NO_PROGRESS)

    def test_budget_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(
            SupervisorPolicy(max_cost_units=2.0)
        )
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.REWORK_REQUIRED,
                criteria_validated=False,
                progress_score=0.2,
                cost_units=2.0,
            ),
            cumulative_cost=2.0,
            elapsed_seconds=1.0,
        )
        self.assertEqual(decision.reason, StopReason.BUDGET_EXHAUSTED)

    def test_external_blocker_stops(self) -> None:
        supervisor = MetacognitiveSupervisor(SupervisorPolicy())
        decision = supervisor.evaluate(
            iteration=1,
            result=CycleResult(
                proof_verdict=Verdict.BLOCKED,
                criteria_validated=False,
                progress_score=0.0,
                cost_units=0.0,
                blocked_reason="Validation du propriétaire requise",
            ),
            cumulative_cost=0.0,
            elapsed_seconds=0.1,
        )
        self.assertEqual(decision.reason, StopReason.EXTERNAL_BLOCKER)
        self.assertTrue(decision.requires_human_decision)


class ReflectiveAgentTests(unittest.TestCase):
    def test_legacy_reflection_report_constructor_remains_valid(self) -> None:
        report = ReflectionReport(
            1,
            ("worked",),
            ("failed",),
            ("gap",),
            "correction",
            "fingerprint",
        )
        self.assertEqual(report.probable_causes, ())
        self.assertEqual(report.hypotheses_to_verify, ())
        self.assertEqual(report.proposed_improvements, ())

    def test_rework_exposes_causes_hypotheses_and_improvements(self) -> None:
        reflector = ReflectiveAgent()
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.4,
            cost_units=1.0,
            evidence=["proof-a"],
            failure_fingerprint="rework-a",
        )

        report = reflector.review(2, result, None)

        self.assertTrue(report.probable_causes)
        self.assertTrue(report.hypotheses_to_verify)
        self.assertTrue(report.proposed_improvements)
        self.assertEqual(
            report.recommended_correction,
            "Réattribuer chaque finding à son propriétaire puis retester uniquement les gates affectées.",
        )
        self.assertIn(
            report.recommended_correction,
            report.proposed_improvements,
        )

    def test_missing_evidence_is_reflected_explicitly(self) -> None:
        reflector = ReflectiveAgent()
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.2,
            cost_units=1.0,
        )

        report = reflector.review(1, result, None)

        self.assertIn("Aucune preuve fournie par le cycle", report.evidence_gaps)
        self.assertIn(
            "Le cycle n'a pas produit de preuve vérifiable.",
            report.probable_causes,
        )
        self.assertIn(
            "Ajouter une preuve vérifiable pour chaque critère d'acceptation affecté.",
            report.proposed_improvements,
        )

    def test_no_progress_adds_root_cause_hypothesis(self) -> None:
        reflector = ReflectiveAgent()
        previous = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.5,
            cost_units=1.0,
            evidence=["proof-before"],
        )
        result = CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=0.5,
            cost_units=1.0,
            evidence=["proof-after"],
        )

        report = reflector.review(2, result, previous)

        self.assertIn(
            "La stratégie appliquée n'a pas augmenté le score de progression.",
            report.probable_causes,
        )
        self.assertIn(
            "La correction actuelle peut ne pas traiter la cause racine.",
            report.hypotheses_to_verify,
        )
        self.assertIn(
            "Changer une seule hypothèse de correction et mesurer son effet au cycle suivant.",
            report.proposed_improvements,
        )

    def test_blocker_remains_advisory_and_requires_no_mutation(self) -> None:
        reflector = ReflectiveAgent()
        result = CycleResult(
            proof_verdict=Verdict.BLOCKED,
            criteria_validated=False,
            progress_score=0.0,
            cost_units=0.0,
            evidence=["dependency-check"],
            blocked_reason="Approval required",
        )
        before = (
            result.proof_verdict,
            result.criteria_validated,
            result.progress_score,
            result.cost_units,
            tuple(result.evidence),
            result.blocked_reason,
        )

        report = reflector.review(1, result, None)

        after = (
            result.proof_verdict,
            result.criteria_validated,
            result.progress_score,
            result.cost_units,
            tuple(result.evidence),
            result.blocked_reason,
        )
        self.assertEqual(before, after)
        self.assertIn("Blocage déclaré: Approval required", report.probable_causes)
        self.assertIn(
            "Escalader le blocage avec les preuves et la décision exacte attendue.",
            report.proposed_improvements,
        )


class MemoryAndLearningTests(unittest.TestCase):
    def test_memory_redacts_secrets(self) -> None:
        memory = MemoryManager()
        memory.remember_working("api_key", "should-not-survive")
        memory.record_episode({"password": "also-secret", "result": "ok"})
        snapshot = memory.snapshot()
        self.assertEqual(snapshot["working"]["api_key"], "[REDACTED]")
        self.assertEqual(snapshot["episodes"][0]["password"], "[REDACTED]")

    def test_learning_requires_explicit_approval(self) -> None:
        learner = LearningAgent()
        reflector = ReflectiveAgent()
        result = CycleResult(
            proof_verdict=Verdict.PASS,
            criteria_validated=True,
            progress_score=1.0,
            cost_units=1.0,
            evidence=["preuve"],
        )
        reflection = reflector.review(1, result, None)
        proposal = learner.propose("wf", result, reflection)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertFalse(proposal.approved)

        memory = MemoryManager()
        memory.approve_lesson(proposal, approved_by="human-reviewer")
        self.assertEqual(len(memory.semantic), 1)


if __name__ == "__main__":
    unittest.main()
