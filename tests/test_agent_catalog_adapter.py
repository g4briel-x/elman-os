from dataclasses import replace
import unittest

from elman_os.agent_catalog_adapter import (
    CATALOG_CONTRACT_VERSION,
    built_in_agent_registry,
    catalog_to_definitions,
    catalog_to_registry,
    profile_to_definition,
)
from elman_os.agent_contracts import (
    AgentContractError,
    RegistryConflictError,
)
from elman_os.catalog import AGENT_CATALOG, get_agent
from elman_os.domain import AgentLayer


class AgentCatalogAdapterTests(unittest.TestCase):
    def test_built_in_registry_preserves_all_21_agent_ids(self) -> None:
        registry = built_in_agent_registry()

        self.assertEqual(len(registry), 21)
        self.assertEqual(
            {definition.agent_id for definition in registry.list()},
            {profile.agent_id for profile in AGENT_CATALOG},
        )

    def test_layer_distribution_is_preserved_in_metadata(self) -> None:
        registry = built_in_agent_registry()
        counts = {
            layer.value: sum(
                definition.to_dict()["metadata"]["layer"] == layer.value
                for definition in registry.list()
            )
            for layer in AgentLayer
        }

        self.assertEqual(counts["orchestration"], 1)
        self.assertEqual(counts["production"], 15)
        self.assertEqual(counts["verification"], 1)
        self.assertEqual(counts["metacognition"], 4)

    def test_profile_fields_are_preserved(self) -> None:
        profile = get_agent("ELMAN_ATLAS")
        definition = profile_to_definition(profile)
        metadata = definition.to_dict()["metadata"]

        self.assertEqual(definition.agent_id, profile.agent_id)
        self.assertEqual(definition.name, profile.name)
        self.assertEqual(definition.role, profile.role)
        self.assertEqual(definition.version, CATALOG_CONTRACT_VERSION)
        self.assertTrue(definition.fail_closed)
        self.assertEqual(
            definition.forbidden_actions,
            tuple(sorted(set(profile.forbidden_actions))),
        )
        self.assertEqual(metadata["mission"], profile.mission)
        self.assertEqual(metadata["layer"], profile.layer.value)
        self.assertEqual(
            metadata["experience_standard"],
            profile.experience_standard,
        )
        self.assertEqual(
            metadata["required_outputs"],
            list(profile.required_outputs),
        )

    def test_scopes_become_capabilities_and_permissions(self) -> None:
        for profile in AGENT_CATALOG:
            with self.subTest(agent_id=profile.agent_id):
                definition = profile_to_definition(profile)
                expected_outputs = tuple(
                    sorted({value.lower() for value in profile.required_outputs})
                )

                self.assertEqual(
                    definition.capability_ids,
                    tuple(sorted(set(profile.allowed_scopes))),
                )
                self.assertEqual(
                    definition.permissions,
                    tuple(sorted(set(profile.allowed_scopes))),
                )

                for capability in definition.capabilities:
                    self.assertEqual(
                        capability.permissions,
                        (capability.capability_id,),
                    )
                    self.assertEqual(
                        capability.output_kinds,
                        expected_outputs,
                    )
                    self.assertFalse(
                        capability.requires_human_approval
                    )

    def test_legacy_output_case_is_adapted_but_preserved(self) -> None:
        profile = get_agent("ELMAN_SCRIBE")
        definition = profile_to_definition(profile)
        metadata = definition.to_dict()["metadata"]

        self.assertIn("README.md", profile.required_outputs)
        self.assertIn("README.md", metadata["required_outputs"])
        self.assertIn(
            "readme.md",
            definition.capabilities[0].output_kinds,
        )
        self.assertNotIn(
            "README.md",
            definition.capabilities[0].output_kinds,
        )

    def test_definitions_are_sorted_by_agent_id(self) -> None:
        definitions = catalog_to_definitions(reversed(AGENT_CATALOG))

        self.assertEqual(
            tuple(item.agent_id for item in definitions),
            tuple(sorted(profile.agent_id for profile in AGENT_CATALOG)),
        )

    def test_registry_json_is_deterministic(self) -> None:
        first = built_in_agent_registry().to_json()
        second = built_in_agent_registry().to_json()

        self.assertEqual(first, second)

    def test_built_in_registry_returns_fresh_instances(self) -> None:
        first = built_in_agent_registry()
        first.unregister("ELMAN_NEXUS")

        second = built_in_agent_registry()

        self.assertNotIn("ELMAN_NEXUS", first)
        self.assertIn("ELMAN_NEXUS", second)
        self.assertEqual(len(second), 21)

    def test_unique_scope_resolves_to_expected_agent(self) -> None:
        registry = built_in_agent_registry()

        selected = registry.resolve(
            "task_routing",
            required_permissions=("task_routing",),
        )

        self.assertEqual(selected.agent_id, "ELMAN_NEXUS")

    def test_duplicate_agent_ids_are_rejected(self) -> None:
        duplicate = AGENT_CATALOG[0]

        with self.assertRaises(RegistryConflictError):
            catalog_to_registry((duplicate, duplicate))

    def test_empty_allowed_scopes_are_rejected(self) -> None:
        invalid = replace(AGENT_CATALOG[0], allowed_scopes=())

        with self.assertRaises(AgentContractError):
            profile_to_definition(invalid)

    def test_invalid_scope_is_rejected_without_normalization(self) -> None:
        invalid = replace(
            AGENT_CATALOG[0],
            allowed_scopes=("Invalid Scope",),
        )

        with self.assertRaises(AgentContractError):
            profile_to_definition(invalid)

    def test_empty_required_outputs_are_rejected(self) -> None:
        invalid = replace(AGENT_CATALOG[0], required_outputs=())

        with self.assertRaises(AgentContractError):
            profile_to_definition(invalid)

    def test_invalid_output_is_rejected_after_case_adaptation(self) -> None:
        invalid = replace(
            AGENT_CATALOG[0],
            required_outputs=("Invalid Output",),
        )

        with self.assertRaises(AgentContractError):
            profile_to_definition(invalid)

    def test_wrong_profile_type_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            profile_to_definition(object())  # type: ignore[arg-type]

    def test_custom_contract_version_is_propagated(self) -> None:
        registry = built_in_agent_registry(version="2.1.0")

        self.assertTrue(
            all(
                definition.version == "2.1.0"
                for definition in registry.list()
            )
        )

    def test_invalid_contract_version_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            built_in_agent_registry(version="v2")


if __name__ == "__main__":
    unittest.main()
