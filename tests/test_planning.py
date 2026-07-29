import unittest

from elman_os.planning import PipelinePlanner, ProjectIntent, ProjectKind


class PlanningTests(unittest.TestCase):
    def test_saas_plan_starts_with_nexus_and_ends_with_proof(self) -> None:
        intent = ProjectIntent(
            name="Task SaaS",
            slug="task-saas",
            kind=ProjectKind.SAAS,
            platforms=("web",),
            features=("paiement",),
        )
        plan = PipelinePlanner().build(intent)
        self.assertEqual(plan.stages[0].agent_ids[0], "ELMAN_NEXUS")
        self.assertEqual(plan.stages[-1].agent_ids, ("ELMAN_PROOF",))
        self.assertEqual(plan.final_verifier, "ELMAN_PROOF")
        self.assertIn("ELMAN_WEB", plan.production_agent_ids)
        self.assertIn("ELMAN_CONNECT", plan.production_agent_ids)
        self.assertNotIn("ELMAN_MOBILE", plan.production_agent_ids)
        self.assertEqual(len(plan.metacognitive_agents), 4)

    def test_fullstack_routes_web_and_mobile(self) -> None:
        intent = ProjectIntent(
            name="Field App",
            slug="field-app",
            kind=ProjectKind.FULLSTACK,
            platforms=("web", "android", "ios"),
        )
        plan = PipelinePlanner().build(intent)
        self.assertIn("ELMAN_WEB", plan.production_agent_ids)
        self.assertIn("ELMAN_MOBILE", plan.production_agent_ids)

    def test_invalid_platform_contract_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProjectIntent(
                name="Broken SaaS",
                slug="broken-saas",
                kind=ProjectKind.SAAS,
                platforms=("android",),
            )

    def test_invalid_slug_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProjectIntent(
                name="Broken",
                slug="../broken",
                kind=ProjectKind.SAAS,
                platforms=("web",),
            )


if __name__ == "__main__":
    unittest.main()

