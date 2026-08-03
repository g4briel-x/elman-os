"""Strict deterministic execution-plan contracts for ELMAN-OS v0.7."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias

from .agent_contracts import (
    AgentContractError,
    AgentRegistry,
    CapabilityResolutionError,
    FrozenJson,
    UnknownAgentError,
    canonical_json,
)


_PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

JsonValue: TypeAlias = str | int | float | bool | None


class ExecutionPlanError(ValueError):
    """An execution plan is malformed or internally inconsistent."""


class PlanCycleError(ExecutionPlanError):
    """The execution graph contains a dependency cycle."""


class UnknownStepError(KeyError):
    """A requested step does not exist in the execution plan."""


class StepStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


class PlanStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPlanError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(
    value: object,
    name: str,
    pattern: re.Pattern[str],
) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise ExecutionPlanError(f"{name} has an invalid format")
    return result


def _tokens(values: Iterable[object], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ExecutionPlanError(f"{name} must be an iterable")
    return tuple(
        sorted(
            {
                _identifier(value, name, _TOKEN)
                for value in values
            }
        )
    )


def _freeze_json(value: Any, path: str = "value") -> FrozenJson:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutionPlanError(
                f"{path} contains a non-finite number"
            )
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExecutionPlanError(
                f"{path} contains a non-string key"
            )
        frozen: dict[str, FrozenJson] = {}
        for raw_key in sorted(value):
            key = _text(raw_key, f"{path} key")
            frozen[key] = _freeze_json(
                value[raw_key],
                f"{path}.{key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise ExecutionPlanError(
        f"{path} contains non-JSON type {type(value).__name__}"
    )


def _thaw_json(value: FrozenJson) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _topological_order(
    steps_by_id: Mapping[str, "ExecutionStep"],
) -> tuple[str, ...]:
    incoming = {
        step_id: set(step.dependencies)
        for step_id, step in steps_by_id.items()
    }
    dependants: dict[str, set[str]] = {
        step_id: set() for step_id in steps_by_id
    }

    for step_id, dependencies in incoming.items():
        for dependency in dependencies:
            dependants[dependency].add(step_id)

    ready = sorted(
        step_id
        for step_id, dependencies in incoming.items()
        if not dependencies
    )
    order: list[str] = []

    while ready:
        current = ready.pop(0)
        order.append(current)

        for dependant in sorted(dependants[current]):
            incoming[dependant].discard(current)
            if not incoming[dependant] and dependant not in order:
                if dependant not in ready:
                    ready.append(dependant)
                    ready.sort()

    if len(order) != len(steps_by_id):
        cyclic = sorted(
            step_id
            for step_id, dependencies in incoming.items()
            if dependencies
        )
        raise PlanCycleError(
            "execution plan contains a dependency cycle: "
            + ", ".join(cyclic)
        )

    return tuple(order)


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    step_id: str
    title: str
    capability_id: str
    objective: str
    dependencies: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    assigned_agent_id: str | None = None
    requires_human_approval: bool = False
    approval_reference: str | None = None
    status: StepStatus = StepStatus.PENDING
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "step_id",
            _identifier(self.step_id, "step_id", _STEP_ID),
        )
        object.__setattr__(
            self,
            "title",
            _text(self.title, "title"),
        )
        object.__setattr__(
            self,
            "capability_id",
            _identifier(
                self.capability_id,
                "capability_id",
                _TOKEN,
            ),
        )
        object.__setattr__(
            self,
            "objective",
            _text(self.objective, "objective"),
        )

        dependencies = _tokens(
            self.dependencies,
            "dependencies",
        )
        if self.step_id in dependencies:
            raise ExecutionPlanError(
                "a step cannot depend on itself"
            )
        object.__setattr__(
            self,
            "dependencies",
            dependencies,
        )

        object.__setattr__(
            self,
            "required_permissions",
            _tokens(
                self.required_permissions,
                "required_permissions",
            ),
        )

        if self.assigned_agent_id is not None:
            object.__setattr__(
                self,
                "assigned_agent_id",
                _identifier(
                    self.assigned_agent_id,
                    "assigned_agent_id",
                    _AGENT_ID,
                ),
            )

        if not isinstance(self.requires_human_approval, bool):
            raise ExecutionPlanError(
                "requires_human_approval must be boolean"
            )

        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _identifier(
                    self.approval_reference,
                    "approval_reference",
                    _PLAN_ID,
                ),
            )

        try:
            status = StepStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ExecutionPlanError(
                "step status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        if (
            self.requires_human_approval
            and status
            in {
                StepStatus.APPROVED,
                StepStatus.RUNNING,
                StepStatus.COMPLETED,
            }
            and self.approval_reference is None
        ):
            raise ExecutionPlanError(
                "approved or active step requires an approval reference"
            )

        if (
            status in {StepStatus.RUNNING, StepStatus.COMPLETED}
            and self.assigned_agent_id is None
        ):
            raise ExecutionPlanError(
                "running or completed step requires an assigned agent"
            )

        frozen = _freeze_json(dict(self.metadata), "metadata")
        if not isinstance(frozen, Mapping):
            raise ExecutionPlanError(
                "metadata must be an object"
            )
        object.__setattr__(self, "metadata", frozen)

    def with_status(
        self,
        status: StepStatus,
        *,
        approval_reference: str | None = None,
    ) -> "ExecutionStep":
        effective_reference = (
            approval_reference
            if approval_reference is not None
            else self.approval_reference
        )
        return replace(
            self,
            status=status,
            approval_reference=effective_reference,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "capability_id": self.capability_id,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "required_permissions": list(
                self.required_permissions
            ),
            "assigned_agent_id": self.assigned_agent_id,
            "requires_human_approval": (
                self.requires_human_approval
            ),
            "approval_reference": self.approval_reference,
            "status": self.status.value,
            "metadata": _thaw_json(self.metadata),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ExecutionStep":
        return cls(
            step_id=data["step_id"],
            title=data["title"],
            capability_id=data["capability_id"],
            objective=data["objective"],
            dependencies=tuple(
                data.get("dependencies", ())
            ),
            required_permissions=tuple(
                data.get("required_permissions", ())
            ),
            assigned_agent_id=data.get(
                "assigned_agent_id"
            ),
            requires_human_approval=data.get(
                "requires_human_approval",
                False,
            ),
            approval_reference=data.get(
                "approval_reference"
            ),
            status=StepStatus(
                data.get("status", StepStatus.PENDING.value)
            ),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionStep":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ExecutionPlanError(
                "execution step JSON must be an object"
            )
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    project_id: str
    objective: str
    created_by: str
    steps: tuple[ExecutionStep, ...]
    status: PlanStatus = PlanStatus.PENDING
    requires_human_approval: bool = True
    approval_reference: str | None = None
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "plan_id", _PLAN_ID),
        )
        object.__setattr__(
            self,
            "project_id",
            _identifier(
                self.project_id,
                "project_id",
                _PLAN_ID,
            ),
        )
        object.__setattr__(
            self,
            "objective",
            _text(self.objective, "objective"),
        )
        object.__setattr__(
            self,
            "created_by",
            _identifier(
                self.created_by,
                "created_by",
                _AGENT_ID,
            ),
        )

        raw_steps = tuple(self.steps)
        if not raw_steps:
            raise ExecutionPlanError(
                "execution plan must contain at least one step"
            )
        if not all(
            isinstance(step, ExecutionStep)
            for step in raw_steps
        ):
            raise ExecutionPlanError(
                "steps must contain ExecutionStep values"
            )

        steps_by_id: dict[str, ExecutionStep] = {}
        for step in raw_steps:
            if step.step_id in steps_by_id:
                raise ExecutionPlanError(
                    f"duplicate step identifier: {step.step_id}"
                )
            steps_by_id[step.step_id] = step

        known_ids = set(steps_by_id)
        for step in raw_steps:
            unknown = set(step.dependencies) - known_ids
            if unknown:
                raise ExecutionPlanError(
                    f"{step.step_id} references unknown dependencies: "
                    + ", ".join(sorted(unknown))
                )

        order = _topological_order(steps_by_id)
        canonical_steps = tuple(
            steps_by_id[step_id]
            for step_id in order
        )
        object.__setattr__(self, "steps", canonical_steps)

        try:
            status = PlanStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ExecutionPlanError(
                "plan status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)

        if not isinstance(self.requires_human_approval, bool):
            raise ExecutionPlanError(
                "requires_human_approval must be boolean"
            )

        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _identifier(
                    self.approval_reference,
                    "approval_reference",
                    _PLAN_ID,
                ),
            )

        if (
            self.requires_human_approval
            and status
            in {
                PlanStatus.APPROVED,
                PlanStatus.RUNNING,
                PlanStatus.COMPLETED,
            }
            and self.approval_reference is None
        ):
            raise ExecutionPlanError(
                "approved or active plan requires an approval reference"
            )

        self._validate_state(status, canonical_steps)

        frozen = _freeze_json(dict(self.metadata), "metadata")
        if not isinstance(frozen, Mapping):
            raise ExecutionPlanError(
                "metadata must be an object"
            )
        object.__setattr__(self, "metadata", frozen)

    @staticmethod
    def _validate_state(
        status: PlanStatus,
        steps: tuple[ExecutionStep, ...],
    ) -> None:
        by_id = {step.step_id: step for step in steps}

        if (
            status is PlanStatus.PENDING
            and any(
                step.status
                in {
                    StepStatus.RUNNING,
                    StepStatus.COMPLETED,
                }
                for step in steps
            )
        ):
            raise ExecutionPlanError(
                "pending plan cannot contain active or completed steps"
            )

        if (
            status is PlanStatus.COMPLETED
            and not all(
                step.status is StepStatus.COMPLETED
                for step in steps
            )
        ):
            raise ExecutionPlanError(
                "completed plan requires all steps to be completed"
            )

        if (
            status is PlanStatus.FAILED
            and not any(
                step.status is StepStatus.FAILED
                for step in steps
            )
        ):
            raise ExecutionPlanError(
                "failed plan requires at least one failed step"
            )

        if (
            status is PlanStatus.BLOCKED
            and not any(
                step.status is StepStatus.BLOCKED
                for step in steps
            )
        ):
            raise ExecutionPlanError(
                "blocked plan requires at least one blocked step"
            )

        for step in steps:
            if step.status not in {
                StepStatus.RUNNING,
                StepStatus.COMPLETED,
            }:
                continue
            incomplete = [
                dependency
                for dependency in step.dependencies
                if by_id[dependency].status
                is not StepStatus.COMPLETED
            ]
            if incomplete:
                raise ExecutionPlanError(
                    f"{step.step_id} has incomplete dependencies: "
                    + ", ".join(sorted(incomplete))
                )

    @property
    def topological_order(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    def get_step(self, step_id: str) -> ExecutionStep:
        normalized = _identifier(
            step_id,
            "step_id",
            _STEP_ID,
        )
        for step in self.steps:
            if step.step_id == normalized:
                return step
        raise UnknownStepError(normalized)

    def ready_steps(self) -> tuple[ExecutionStep, ...]:
        completed = {
            step.step_id
            for step in self.steps
            if step.status is StepStatus.COMPLETED
        }
        return tuple(
            step
            for step in self.steps
            if step.status
            in {
                StepStatus.PENDING,
                StepStatus.APPROVED,
            }
            and set(step.dependencies).issubset(completed)
        )

    def approve(
        self,
        approval_reference: str,
    ) -> "ExecutionPlan":
        return replace(
            self,
            status=PlanStatus.APPROVED,
            approval_reference=approval_reference,
        )

    def bind_agents(
        self,
        registry: AgentRegistry,
    ) -> "ExecutionPlan":
        if not isinstance(registry, AgentRegistry):
            raise ExecutionPlanError(
                "registry must be an AgentRegistry"
            )

        bound_steps: list[ExecutionStep] = []

        for step in self.steps:
            effective_approval = (
                step.approval_reference
                or self.approval_reference
            )

            if step.assigned_agent_id is None:
                definition = registry.resolve(
                    step.capability_id,
                    required_permissions=(
                        step.required_permissions
                    ),
                    approval_reference=effective_approval,
                )
            else:
                try:
                    definition = registry.get(
                        step.assigned_agent_id
                    )
                except UnknownAgentError as exc:
                    raise CapabilityResolutionError(
                        "assigned agent is not registered: "
                        f"{step.assigned_agent_id}"
                    ) from exc

                if not definition.supports(
                    step.capability_id
                ):
                    raise CapabilityResolutionError(
                        f"{definition.agent_id} does not support "
                        f"{step.capability_id}"
                    )

                required = set(
                    step.required_permissions
                )
                if not required.issubset(
                    definition.permissions
                ):
                    raise CapabilityResolutionError(
                        f"{definition.agent_id} lacks required permissions"
                    )

                capability = definition.capability(
                    step.capability_id
                )
                if (
                    capability.requires_human_approval
                    and effective_approval is None
                ):
                    raise CapabilityResolutionError(
                        "capability requires human approval"
                    )

            capability = definition.capability(
                step.capability_id
            )
            trace_approval = (
                effective_approval
                if (
                    step.requires_human_approval
                    or capability.requires_human_approval
                )
                else step.approval_reference
            )

            bound_steps.append(
                replace(
                    step,
                    assigned_agent_id=definition.agent_id,
                    approval_reference=trace_approval,
                )
            )

        return replace(
            self,
            steps=tuple(bound_steps),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "objective": self.objective,
            "created_by": self.created_by,
            "steps": [
                step.to_dict()
                for step in self.steps
            ],
            "status": self.status.value,
            "requires_human_approval": (
                self.requires_human_approval
            ),
            "approval_reference": self.approval_reference,
            "metadata": _thaw_json(self.metadata),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ExecutionPlan":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise ExecutionPlanError(
                "execution plan steps must be a list"
            )
        return cls(
            plan_id=data["plan_id"],
            project_id=data["project_id"],
            objective=data["objective"],
            created_by=data["created_by"],
            steps=tuple(
                ExecutionStep.from_dict(step)
                for step in raw_steps
            ),
            status=PlanStatus(
                data.get("status", PlanStatus.PENDING.value)
            ),
            requires_human_approval=data.get(
                "requires_human_approval",
                True,
            ),
            approval_reference=data.get(
                "approval_reference"
            ),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionPlan":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ExecutionPlanError(
                "execution plan JSON must be an object"
            )
        return cls.from_dict(data)
