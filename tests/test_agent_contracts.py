import json
import math
import unittest

from elman_os.agent_contracts import (
    AgentCapability,
    AgentContractError,
    AgentDefinition,
    AgentRegistry,
    AgentRequest,
    AgentResponse,
    AgentResponseStatus,
    CapabilityResolutionError,
    RegistryConflictError,
    UnknownAgentError,
    canonical_json,
)


def make_capability(
    capability_id: str = "requirements.analyze",
    *,
    approval: bool = False,
) -> AgentCapability:
    return AgentCapability(
        capability_id=capability_id,
        description="Analyze validated requirements",
        input_kinds=("project.brief",),
        output_kinds=("requirements.spec",),
        permissions=("project.read",),
        requires_human_approval=approval,
    )


def make_definition(
    agent_id: str = "ELMAN_DISCOVERY",
    capability_id: str = "requirements.analyze",
    *,
    approval: bool = False,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id.replace("_", " ").title(),
        role="Specialized agent",
        version="1.0.0",
        capabilities=(make_capability(capability_id, approval=approval),),
        permissions=("project.read",),
        forbidden_actions=("production.deploy",),
        metadata={"priority": 10, "labels": ["local", "offline"]},
    )


class CapabilityTests(unittest.TestCase):
    def test_values_are_canonicalized(self) -> None:
        item = AgentCapability(
            "requirements.analyze",
            "  Analyze requirements  ",
            input_kinds=("project.brief", "project.brief"),
            permissions=("project.read", "project.read"),
        )
        self.assertEqual(item.description, "Analyze requirements")
        self.assertEqual(item.input_kinds, ("project.brief",))
        self.assertEqual(item.permissions, ("project.read",))

    def test_invalid_identifier_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            make_capability("Requirements Analyze")

    def test_approval_flag_must_be_boolean(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentCapability(
                "requirements.analyze",
                "Analyze requirements",
                requires_human_approval="yes",  # type: ignore[arg-type]
            )

    def test_round_trip(self) -> None:
        item = make_capability()
        self.assertEqual(AgentCapability.from_dict(item.to_dict()), item)


class DefinitionTests(unittest.TestCase):
    def test_capabilities_are_sorted(self) -> None:
        item = AgentDefinition(
            "ELMAN_ATLAS",
            "ELMAN Atlas",
            "Architecture",
            "1.2.3",
            (
                make_capability("system.plan"),
                make_capability("architecture.review"),
            ),
            permissions=("project.read",),
        )
        self.assertEqual(
            item.capability_ids,
            ("architecture.review", "system.plan"),
        )

    def test_duplicate_capabilities_are_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition(
                "ELMAN_ATLAS",
                "ELMAN Atlas",
                "Architecture",
                "1.0.0",
                (make_capability(), make_capability()),
                permissions=("project.read",),
            )

    def test_capability_permissions_must_be_declared(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition(
                "ELMAN_ATLAS",
                "ELMAN Atlas",
                "Architecture",
                "1.0.0",
                (make_capability(),),
            )

    def test_allowed_and_forbidden_cannot_overlap(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition(
                "ELMAN_ATLAS",
                "ELMAN Atlas",
                "Architecture",
                "1.0.0",
                (make_capability(),),
                permissions=("project.read",),
                forbidden_actions=("project.read",),
            )

    def test_non_capability_value_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition(
                "ELMAN_ATLAS",
                "ELMAN Atlas",
                "Architecture",
                "1.0.0",
                ("not-a-capability",),  # type: ignore[arg-type]
                permissions=("project.read",),
            )

    def test_invalid_semver_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition(
                "ELMAN_ATLAS",
                "ELMAN Atlas",
                "Architecture",
                "v1",
                (make_capability(),),
                permissions=("project.read",),
            )

    def test_metadata_is_deeply_immutable(self) -> None:
        source = {"nested": {"items": [1, 2]}}
        item = AgentDefinition(
            "ELMAN_ATLAS",
            "ELMAN Atlas",
            "Architecture",
            "1.0.0",
            (make_capability(),),
            permissions=("project.read",),
            metadata=source,
        )
        source["nested"]["items"].append(3)
        self.assertEqual(
            item.to_dict()["metadata"],
            {"nested": {"items": [1, 2]}},
        )
        with self.assertRaises(TypeError):
            item.metadata["new"] = "value"  # type: ignore[index]

    def test_json_is_deterministic_and_round_trips(self) -> None:
        payload = make_definition().to_json()
        self.assertEqual(payload, make_definition().to_json())
        self.assertEqual(
            AgentDefinition.from_json(payload),
            make_definition(),
        )

    def test_capability_lookup_fails_closed(self) -> None:
        item = make_definition()
        self.assertTrue(item.supports("requirements.analyze"))
        with self.assertRaises(CapabilityResolutionError):
            item.capability("unknown.capability")


class RequestTests(unittest.TestCase):
    def test_round_trip_and_immutability(self) -> None:
        source = {"brief": {"features": ["auth"]}}
        request = AgentRequest(
            "req-001",
            "project-001",
            "requirements.analyze",
            "Produce measurable requirements",
            "ELMAN_NEXUS",
            inputs=source,
            constraints={"offline": True},
        )
        source["brief"]["features"].append("billing")
        self.assertEqual(
            request.to_dict()["inputs"],
            {"brief": {"features": ["auth"]}},
        )
        self.assertEqual(AgentRequest.from_json(request.to_json()), request)

    def test_non_string_json_key_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentRequest(
                "req-001",
                "project-001",
                "requirements.analyze",
                "Analyze requirements",
                "ELMAN_NEXUS",
                inputs={1: "invalid"},  # type: ignore[dict-item]
            )

    def test_non_json_input_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentRequest(
                "req-001",
                "project-001",
                "requirements.analyze",
                "Analyze requirements",
                "ELMAN_NEXUS",
                inputs={"invalid": object()},
            )

    def test_non_finite_number_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentRequest(
                "req-001",
                "project-001",
                "requirements.analyze",
                "Analyze requirements",
                "ELMAN_NEXUS",
                inputs={"score": math.inf},
            )

    def test_invalid_approval_reference_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentRequest(
                "req-001",
                "project-001",
                "requirements.analyze",
                "Analyze requirements",
                "ELMAN_NEXUS",
                approval_reference=" ",
            )


class ResponseTests(unittest.TestCase):
    def test_success_round_trip(self) -> None:
        response = AgentResponse(
            "req-001",
            "ELMAN_DISCOVERY",
            AgentResponseStatus.SUCCEEDED,
            "Requirements validated",
            outputs={"artifact": "requirements.md"},
            evidence=("tests passed",),
            confidence=0.95,
            next_handoff="ELMAN_ATLAS",
        )
        self.assertEqual(AgentResponse.from_json(response.to_json()), response)

    def test_success_cannot_contain_errors(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentResponse(
                "req-001",
                "ELMAN_DISCOVERY",
                AgentResponseStatus.SUCCEEDED,
                "Invalid success",
                errors=("failure",),
            )

    def test_failure_requires_error(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentResponse(
                "req-001",
                "ELMAN_DISCOVERY",
                AgentResponseStatus.FAILED,
                "Execution failed",
            )

    def test_blocked_requires_explanation(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentResponse(
                "req-001",
                "ELMAN_DISCOVERY",
                AgentResponseStatus.BLOCKED,
                "Execution blocked",
            )

    def test_confidence_is_bounded(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentResponse(
                "req-001",
                "ELMAN_DISCOVERY",
                AgentResponseStatus.SUCCEEDED,
                "Invalid confidence",
                confidence=1.1,
            )


class RegistryTests(unittest.TestCase):
    def test_listing_is_deterministic(self) -> None:
        registry = AgentRegistry(
            (
                make_definition("ELMAN_WEB", "web.build"),
                make_definition("ELMAN_ATLAS", "architecture.plan"),
            )
        )
        self.assertEqual(
            tuple(item.agent_id for item in registry.list()),
            ("ELMAN_ATLAS", "ELMAN_WEB"),
        )

    def test_duplicate_registration_fails(self) -> None:
        registry = AgentRegistry((make_definition(),))
        with self.assertRaises(RegistryConflictError):
            registry.register(make_definition())

    def test_replace_is_explicit(self) -> None:
        registry = AgentRegistry((make_definition(),))
        registry.register(
            make_definition(capability_id="requirements.review"),
            replace=True,
        )
        self.assertTrue(
            registry.get("ELMAN_DISCOVERY").supports("requirements.review")
        )

    def test_unknown_agent_fails(self) -> None:
        with self.assertRaises(UnknownAgentError):
            AgentRegistry().get("ELMAN_UNKNOWN")

    def test_find_by_capability(self) -> None:
        registry = AgentRegistry(
            (
                make_definition("ELMAN_WEB", "web.build"),
                make_definition("ELMAN_ATLAS", "architecture.plan"),
            )
        )
        self.assertEqual(
            tuple(
                item.agent_id
                for item in registry.find_by_capability("architecture.plan")
            ),
            ("ELMAN_ATLAS",),
        )

    def test_resolution_is_deterministic(self) -> None:
        registry = AgentRegistry(
            (
                make_definition("ELMAN_WEB", "project.review"),
                make_definition("ELMAN_ATLAS", "project.review"),
            )
        )
        self.assertEqual(
            registry.resolve("project.review").agent_id,
            "ELMAN_ATLAS",
        )

    def test_resolution_enforces_permissions(self) -> None:
        registry = AgentRegistry((make_definition(),))
        with self.assertRaises(CapabilityResolutionError):
            registry.resolve(
                "requirements.analyze",
                required_permissions=("project.write",),
            )

    def test_resolution_enforces_approval(self) -> None:
        registry = AgentRegistry((make_definition(approval=True),))
        with self.assertRaises(CapabilityResolutionError):
            registry.resolve("requirements.analyze")
        self.assertEqual(
            registry.resolve(
                "requirements.analyze",
                approval_reference="approval-001",
            ).agent_id,
            "ELMAN_DISCOVERY",
        )

    def test_registry_round_trip(self) -> None:
        registry = AgentRegistry(
            (
                make_definition("ELMAN_WEB", "web.build"),
                make_definition("ELMAN_ATLAS", "architecture.plan"),
            )
        )
        restored = AgentRegistry.from_json(registry.to_json())
        self.assertEqual(restored.to_dict(), registry.to_dict())

    def test_unregister_returns_definition(self) -> None:
        registry = AgentRegistry((make_definition(),))
        removed = registry.unregister("ELMAN_DISCOVERY")
        self.assertEqual(removed.agent_id, "ELMAN_DISCOVERY")
        self.assertEqual(len(registry), 0)


class CanonicalJsonTests(unittest.TestCase):
    def test_keys_are_sorted(self) -> None:
        self.assertEqual(canonical_json({"z": 1, "a": 2}), '{"a":2,"z":1}')

    def test_nan_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            canonical_json({"score": float("nan")})

    def test_payloads_must_be_objects(self) -> None:
        with self.assertRaises(AgentContractError):
            AgentDefinition.from_json(json.dumps([]))
        with self.assertRaises(AgentContractError):
            AgentRequest.from_json(json.dumps([]))
        with self.assertRaises(AgentContractError):
            AgentResponse.from_json(json.dumps([]))
        with self.assertRaises(AgentContractError):
            AgentRegistry.from_json(json.dumps([]))


if __name__ == "__main__":
    unittest.main()
