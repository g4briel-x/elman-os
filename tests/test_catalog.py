import unittest

from elman_os.catalog import AGENT_CATALOG, agents_by_layer, get_agent, system_prompt
from elman_os.domain import AgentLayer


class CatalogTests(unittest.TestCase):
    def test_expected_agent_counts(self) -> None:
        self.assertEqual(len(AGENT_CATALOG), 21)
        self.assertEqual(len(agents_by_layer(AgentLayer.ORCHESTRATION)), 1)
        self.assertEqual(len(agents_by_layer(AgentLayer.PRODUCTION)), 15)
        self.assertEqual(len(agents_by_layer(AgentLayer.VERIFICATION)), 1)
        self.assertEqual(len(agents_by_layer(AgentLayer.METACOGNITION)), 4)

    def test_agent_ids_are_unique(self) -> None:
        ids = [agent.agent_id for agent in AGENT_CATALOG]
        self.assertEqual(len(ids), len(set(ids)))

    def test_prompt_includes_role_boundaries(self) -> None:
        prompt = system_prompt(get_agent("ELMAN_PROOF"))
        self.assertIn("Vérificateur final indépendant", prompt)
        self.assertIn("approve_own_authored_code", prompt)
        self.assertIn("non_python_core_source", prompt)
        self.assertIn("15 années", prompt)

    def test_python_core_rule_applies_to_every_agent(self) -> None:
        for agent in AGENT_CATALOG:
            with self.subTest(agent=agent.agent_id):
                self.assertIn(
                    "non_python_core_source",
                    agent.forbidden_actions,
                )

    def test_unknown_agent_fails(self) -> None:
        with self.assertRaises(KeyError):
            get_agent("UNKNOWN")


if __name__ == "__main__":
    unittest.main()
