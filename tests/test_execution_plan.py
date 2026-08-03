from dataclasses import FrozenInstanceError
import math
import unittest

from elman_os.agent_contracts import (
    AgentCapability,
    AgentDefinition,
    AgentRegistry,
    CapabilityResolutionError,
)
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionPlanError,
    ExecutionStep,
    PlanCycleError,
    PlanStatus,
    StepStatus,
    UnknownStepError,
)


def agent_definition(
    agent_id: str,
    capability_id: str,
    *,
    permissions: tuple[str, ...] | None = None,
    requires_human_approval: bool = False,
) -> AgentDefinition:
    declared_permissions = (
        permissions
        if permissions is not None
        else (capability_id,)
    )
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id,
        role="Test agent",
        version="1.0.0",
        capabilities=(
            AgentCapability(
                capability_id=capability_id,
                description="Test capability",
                permissions=(capability_id,),
                requires_human_approval=(
                    requires_human_approval
                ),
            ),
        ),
        permissions=declared_permissions,
    )


def execution_step(
    step_id: str = "step.one",
    *,
    capability_id: str = "build",
    dependencies: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    assigned_agent_id: str | None = None,
    requires_human_approval: bool = False,
    approval_reference: str | None = None,
    status: StepStatus = StepStatus.PENDING,
    metadata: dict[str, object] | None = None,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        title=f"Title for {step_id}",
        capability_id=capability_id,
        objective=f"Objective for {step_id}",
        dependencies=dependencies,
        required_permissions=required_permissions,
        assigned_agent_id=assigned_agent_id,
        requires_human_approval=requires_human_approval,
        approval_reference=approval_reference,
        status=status,
        metadata=metadata or {},
    )


def execution_plan(
    steps: tuple[ExecutionStep, ...],
    *,
    status: PlanStatus = PlanStatus.PENDING,
    requires_human_approval: bool = True,
    approval_reference: str | None = None,
    metadata: dict[str, object] | None = None,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan:001",
        project_id="project:001",
        objective="Deliver a deterministic plan",
        created_by="ELMAN_NEXUS",
        steps=steps,
        status=status,
        requires_human_approval=requires_human_approval,
        approval_reference=approval_reference,
        metadata=metadata or {},
    )


class ExecutionStepTests(unittest.TestCase):
    def test_step_normalizes_dependencies_and_permissions(self) -> None:
        step = execution_step(
            dependencies=("step.z", "step.a", "step.z"),
            required_permissions=("secure", "build", "secure"),
        )

        self.assertEqual(
            step.dependencies,
            ("step.a", "step.z"),
        )
        self.assertEqual(
            step.required_permissions,
            ("build", "secure"),
        )

    def test_step_rejects_invalid_identifier(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(step_id="Invalid Step")

    def test_step_rejects_self_dependency(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(
                step_id="step.one",
                dependencies=("step.one",),
            )

    def test_step_rejects_invalid_capability(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(capability_id="Invalid Capability")

    def test_step_rejects_invalid_assigned_agent(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(assigned_agent_id="agent-lower")

    def test_step_approval_required_for_approved_status(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(
                requires_human_approval=True,
                status=StepStatus.APPROVED,
            )

    def test_step_running_requires_agent(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(status=StepStatus.RUNNING)

    def test_step_completed_requires_agent(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(status=StepStatus.COMPLETED)

    def test_step_json_round_trip(self) -> None:
        original = execution_step(
            required_permissions=("build",),
            metadata={"priority": 1, "tags": ["safe"]},
        )

        restored = ExecutionStep.from_json(original.to_json())

        self.assertEqual(restored, original)
        self.assertEqual(restored.to_json(), original.to_json())

    def test_step_metadata_is_immutable(self) -> None:
        step = execution_step(metadata={"nested": {"value": 1}})

        with self.assertRaises(TypeError):
            step.metadata["new"] = "value"  # type: ignore[index]

        nested = step.metadata["nested"]
        with self.assertRaises(TypeError):
            nested["value"] = 2  # type: ignore[index]

    def test_step_rejects_non_json_metadata(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_step(metadata={"bad": object()})

    def test_step_with_status_returns_new_instance(self) -> None:
        original = execution_step(
            assigned_agent_id="AGENT_ALPHA",
        )

        updated = original.with_status(StepStatus.RUNNING)

        self.assertIs(original.status, StepStatus.PENDING)
        self.assertIs(updated.status, StepStatus.RUNNING)
        self.assertIsNot(original, updated)

    def test_step_dataclass_is_frozen(self) -> None:
        step = execution_step()

        with self.assertRaises(FrozenInstanceError):
            step.title = "Changed"  # type: ignore[misc]


class ExecutionPlanValidationTests(unittest.TestCase):
    def test_plan_requires_steps(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_plan(())

    def test_plan_rejects_duplicate_step_ids(self) -> None:
        duplicate = execution_step("step.one")

        with self.assertRaises(ExecutionPlanError):
            execution_plan((duplicate, duplicate))

    def test_plan_rejects_unknown_dependency(self) -> None:
        step = execution_step(
            "step.one",
            dependencies=("step.missing",),
        )

        with self.assertRaises(ExecutionPlanError):
            execution_plan((step,))

    def test_plan_rejects_dependency_cycle(self) -> None:
        first = execution_step(
            "step.a",
            dependencies=("step.b",),
        )
        second = execution_step(
            "step.b",
            dependencies=("step.a",),
        )

        with self.assertRaises(PlanCycleError):
            execution_plan((first, second))

    def test_plan_topological_order_is_deterministic(self) -> None:
        first = execution_step("step.a")
        second = execution_step("step.b")
        final = execution_step(
            "step.c",
            dependencies=("step.b", "step.a"),
        )

        plan = execution_plan((final, second, first))

        self.assertEqual(
            plan.topological_order,
            ("step.a", "step.b", "step.c"),
        )

    def test_plan_places_dependencies_before_dependants(self) -> None:
        root = execution_step("step.root")
        middle = execution_step(
            "step.middle",
            dependencies=("step.root",),
        )
        final = execution_step(
            "step.final",
            dependencies=("step.middle",),
        )

        plan = execution_plan((final, root, middle))

        self.assertEqual(
            plan.topological_order,
            ("step.root", "step.middle", "step.final"),
        )

    def test_plan_approved_requires_reference(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (execution_step(),),
                status=PlanStatus.APPROVED,
            )

    def test_pending_plan_rejects_completed_step(self) -> None:
        completed = execution_step(
            assigned_agent_id="AGENT_ALPHA",
            status=StepStatus.COMPLETED,
        )

        with self.assertRaises(ExecutionPlanError):
            execution_plan((completed,))

    def test_completed_plan_requires_all_steps_completed(self) -> None:
        completed = execution_step(
            "step.done",
            assigned_agent_id="AGENT_ALPHA",
            status=StepStatus.COMPLETED,
        )
        pending = execution_step("step.pending")

        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (completed, pending),
                status=PlanStatus.COMPLETED,
                requires_human_approval=False,
            )

    def test_failed_plan_requires_failed_step(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (execution_step(),),
                status=PlanStatus.FAILED,
                requires_human_approval=False,
            )

    def test_blocked_plan_requires_blocked_step(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (execution_step(),),
                status=PlanStatus.BLOCKED,
                requires_human_approval=False,
            )

    def test_running_step_requires_completed_dependencies(self) -> None:
        root = execution_step("step.root")
        dependant = execution_step(
            "step.dependant",
            dependencies=("step.root",),
            assigned_agent_id="AGENT_ALPHA",
            status=StepStatus.RUNNING,
        )

        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (root, dependant),
                status=PlanStatus.RUNNING,
                requires_human_approval=False,
            )

    def test_plan_rejects_non_finite_metadata(self) -> None:
        with self.assertRaises(ExecutionPlanError):
            execution_plan(
                (execution_step(),),
                metadata={"score": math.inf},
            )

    def test_plan_dataclass_is_frozen(self) -> None:
        plan = execution_plan((execution_step(),))

        with self.assertRaises(FrozenInstanceError):
            plan.objective = "Changed"  # type: ignore[misc]


class ExecutionPlanBehaviorTests(unittest.TestCase):
    def test_plan_json_round_trip(self) -> None:
        root = execution_step("step.root")
        final = execution_step(
            "step.final",
            dependencies=("step.root",),
        )
        original = execution_plan(
            (final, root),
            metadata={"source": "test"},
        )

        restored = ExecutionPlan.from_json(original.to_json())

        self.assertEqual(restored, original)
        self.assertEqual(restored.to_json(), original.to_json())

    def test_plan_json_independent_of_input_step_order(self) -> None:
        first = execution_step("step.a")
        second = execution_step("step.b")
        final = execution_step(
            "step.c",
            dependencies=("step.a", "step.b"),
        )

        left = execution_plan((first, second, final))
        right = execution_plan((final, second, first))

        self.assertEqual(left.to_json(), right.to_json())

    def test_plan_approve_returns_new_plan(self) -> None:
        original = execution_plan((execution_step(),))

        approved = original.approve("approval:001")

        self.assertIs(original.status, PlanStatus.PENDING)
        self.assertIs(approved.status, PlanStatus.APPROVED)
        self.assertEqual(
            approved.approval_reference,
            "approval:001",
        )

    def test_plan_approve_rejects_invalid_reference(self) -> None:
        plan = execution_plan((execution_step(),))

        with self.assertRaises(ExecutionPlanError):
            plan.approve("bad reference")

    def test_ready_steps_returns_dependency_frontier(self) -> None:
        root = execution_step(
            "step.root",
            assigned_agent_id="AGENT_ALPHA",
            status=StepStatus.COMPLETED,
        )
        middle = execution_step(
            "step.middle",
            dependencies=("step.root",),
        )
        final = execution_step(
            "step.final",
            dependencies=("step.middle",),
        )
        plan = execution_plan(
            (final, middle, root),
            status=PlanStatus.RUNNING,
            requires_human_approval=False,
        )

        self.assertEqual(
            tuple(step.step_id for step in plan.ready_steps()),
            ("step.middle",),
        )

    def test_get_step_unknown(self) -> None:
        plan = execution_plan((execution_step(),))

        with self.assertRaises(UnknownStepError):
            plan.get_step("step.missing")


class AgentBindingTests(unittest.TestCase):
    def test_bind_agents_selects_lexicographically_first(self) -> None:
        registry = AgentRegistry(
            (
                agent_definition("AGENT_BETA", "build"),
                agent_definition("AGENT_ALPHA", "build"),
            )
        )
        plan = execution_plan((execution_step(),))

        bound = plan.bind_agents(registry)

        self.assertEqual(
            bound.steps[0].assigned_agent_id,
            "AGENT_ALPHA",
        )

    def test_bind_agents_respects_required_permissions(self) -> None:
        registry = AgentRegistry(
            (
                agent_definition(
                    "AGENT_ALPHA",
                    "build",
                    permissions=("build",),
                ),
                agent_definition(
                    "AGENT_BETA",
                    "build",
                    permissions=("build", "secure"),
                ),
            )
        )
        plan = execution_plan(
            (
                execution_step(
                    required_permissions=("secure",),
                ),
            )
        )

        bound = plan.bind_agents(registry)

        self.assertEqual(
            bound.steps[0].assigned_agent_id,
            "AGENT_BETA",
        )

    def test_bind_agents_rejects_unknown_capability(self) -> None:
        registry = AgentRegistry(
            (agent_definition("AGENT_ALPHA", "build"),)
        )
        plan = execution_plan(
            (execution_step(capability_id="deploy"),)
        )

        with self.assertRaises(CapabilityResolutionError):
            plan.bind_agents(registry)

    def test_bind_agents_preserves_valid_explicit_assignment(self) -> None:
        registry = AgentRegistry(
            (agent_definition("AGENT_ALPHA", "build"),)
        )
        plan = execution_plan(
            (
                execution_step(
                    assigned_agent_id="AGENT_ALPHA",
                ),
            )
        )

        bound = plan.bind_agents(registry)

        self.assertEqual(
            bound.steps[0].assigned_agent_id,
            "AGENT_ALPHA",
        )

    def test_bind_agents_rejects_invalid_explicit_assignment(self) -> None:
        registry = AgentRegistry(
            (agent_definition("AGENT_ALPHA", "test"),)
        )
        plan = execution_plan(
            (
                execution_step(
                    assigned_agent_id="AGENT_ALPHA",
                ),
            )
        )

        with self.assertRaises(CapabilityResolutionError):
            plan.bind_agents(registry)

    def test_capability_approval_can_use_plan_approval(self) -> None:
        registry = AgentRegistry(
            (
                agent_definition(
                    "AGENT_ALPHA",
                    "build",
                    requires_human_approval=True,
                ),
            )
        )
        plan = execution_plan((execution_step(),))
        approved = plan.approve("approval:001")

        bound = approved.bind_agents(registry)

        self.assertEqual(
            bound.steps[0].assigned_agent_id,
            "AGENT_ALPHA",
        )
        self.assertEqual(
            bound.steps[0].approval_reference,
            "approval:001",
        )

    def test_capability_approval_without_reference_fails(self) -> None:
        registry = AgentRegistry(
            (
                agent_definition(
                    "AGENT_ALPHA",
                    "build",
                    requires_human_approval=True,
                ),
            )
        )
        plan = execution_plan((execution_step(),))

        with self.assertRaises(CapabilityResolutionError):
            plan.bind_agents(registry)

    def test_bind_agents_rejects_non_registry(self) -> None:
        plan = execution_plan((execution_step(),))

        with self.assertRaises(ExecutionPlanError):
            plan.bind_agents(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
