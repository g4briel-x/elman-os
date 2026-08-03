"""Strict, deterministic contracts for ELMAN-OS multi-agent execution."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

_AGENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class AgentContractError(ValueError):
    """A contract is malformed or internally inconsistent."""


class RegistryConflictError(AgentContractError):
    """A registry mutation would replace an existing agent."""


class UnknownAgentError(KeyError):
    """An agent identifier is absent from the registry."""


class CapabilityResolutionError(LookupError):
    """No registered agent can satisfy a capability request."""


class AgentResponseStatus(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentContractError(f"{name} must be a non-empty string")
    return value.strip()


def _id(value: object, name: str, pattern: re.Pattern[str]) -> str:
    result = _text(value, name)
    if pattern.fullmatch(result) is None:
        raise AgentContractError(f"{name} has an invalid format")
    return result


def _tokens(values: Iterable[object], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentContractError(f"{name} must be an iterable")
    return tuple(sorted({_id(value, name, _TOKEN) for value in values}))


def _texts(values: Iterable[object], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentContractError(f"{name} must be an iterable")
    return tuple(dict.fromkeys(_text(value, name) for value in values))


def _freeze(value: Any, path: str = "value") -> FrozenJson:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AgentContractError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(raw_key, str) for raw_key in value):
            raise AgentContractError(f"{path} contains a non-string key")
        frozen: dict[str, FrozenJson] = {}
        for raw_key in sorted(value):
            key = _text(raw_key, f"{path} key")
            frozen[key] = _freeze(value[raw_key], f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{path}[{index}]") for index, item in enumerate(value))
    raise AgentContractError(f"{path} contains non-JSON type {type(value).__name__}")


def _thaw(value: FrozenJson) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def canonical_json(data: Mapping[str, Any]) -> str:
    """Return stable UTF-8 JSON and reject non-finite numbers."""

    return json.dumps(
        _thaw(_freeze(data, "document")),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class AgentCapability:
    capability_id: str
    description: str
    input_kinds: tuple[str, ...] = ()
    output_kinds: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    requires_human_approval: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _id(self.capability_id, "capability_id", _TOKEN))
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "input_kinds", _tokens(self.input_kinds, "input_kinds"))
        object.__setattr__(self, "output_kinds", _tokens(self.output_kinds, "output_kinds"))
        object.__setattr__(self, "permissions", _tokens(self.permissions, "permissions"))
        if not isinstance(self.requires_human_approval, bool):
            raise AgentContractError("requires_human_approval must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "description": self.description,
            "input_kinds": list(self.input_kinds),
            "output_kinds": list(self.output_kinds),
            "permissions": list(self.permissions),
            "requires_human_approval": self.requires_human_approval,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentCapability":
        return cls(
            capability_id=data["capability_id"],
            description=data["description"],
            input_kinds=tuple(data.get("input_kinds", ())),
            output_kinds=tuple(data.get("output_kinds", ())),
            permissions=tuple(data.get("permissions", ())),
            requires_human_approval=data.get("requires_human_approval", False),
        )


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    role: str
    version: str
    capabilities: tuple[AgentCapability, ...]
    permissions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    fail_closed: bool = True
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _id(self.agent_id, "agent_id", _AGENT_ID))
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "role", _text(self.role, "role"))
        version = _text(self.version, "version")
        if _SEMVER.fullmatch(version) is None:
            raise AgentContractError("version must use semantic versioning")
        object.__setattr__(self, "version", version)

        raw_capabilities = tuple(self.capabilities)
        if not raw_capabilities or not all(
            isinstance(item, AgentCapability) for item in raw_capabilities
        ):
            raise AgentContractError("capabilities must contain AgentCapability values")
        capabilities = tuple(
            sorted(raw_capabilities, key=lambda item: item.capability_id)
        )
        ids = tuple(item.capability_id for item in capabilities)
        if len(ids) != len(set(ids)):
            raise AgentContractError("capability identifiers must be unique")
        object.__setattr__(self, "capabilities", capabilities)

        permissions = _tokens(self.permissions, "permissions")
        forbidden = _tokens(self.forbidden_actions, "forbidden_actions")
        overlap = set(permissions) & set(forbidden)
        if overlap:
            raise AgentContractError("allowed and forbidden actions overlap")
        required = {permission for item in capabilities for permission in item.permissions}
        missing = required - set(permissions)
        if missing:
            raise AgentContractError("capability permissions are not declared by the agent")
        object.__setattr__(self, "permissions", permissions)
        object.__setattr__(self, "forbidden_actions", forbidden)

        if not isinstance(self.fail_closed, bool):
            raise AgentContractError("fail_closed must be boolean")
        frozen = _freeze(dict(self.metadata), "metadata")
        if not isinstance(frozen, Mapping):
            raise AgentContractError("metadata must be an object")
        object.__setattr__(self, "metadata", frozen)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(item.capability_id for item in self.capabilities)

    def supports(self, capability_id: str) -> bool:
        return _id(capability_id, "capability_id", _TOKEN) in self.capability_ids

    def capability(self, capability_id: str) -> AgentCapability:
        normalized = _id(capability_id, "capability_id", _TOKEN)
        for item in self.capabilities:
            if item.capability_id == normalized:
                return item
        raise CapabilityResolutionError(f"{self.agent_id} does not expose {normalized}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "version": self.version,
            "capabilities": [item.to_dict() for item in self.capabilities],
            "permissions": list(self.permissions),
            "forbidden_actions": list(self.forbidden_actions),
            "fail_closed": self.fail_closed,
            "metadata": _thaw(self.metadata),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentDefinition":
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            role=data["role"],
            version=data["version"],
            capabilities=tuple(AgentCapability.from_dict(item) for item in data["capabilities"]),
            permissions=tuple(data.get("permissions", ())),
            forbidden_actions=tuple(data.get("forbidden_actions", ())),
            fail_closed=data.get("fail_closed", True),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> "AgentDefinition":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise AgentContractError("agent definition JSON must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class AgentRequest:
    request_id: str
    project_id: str
    capability_id: str
    objective: str
    requested_by: str
    inputs: Mapping[str, FrozenJson] = field(default_factory=dict)
    constraints: Mapping[str, FrozenJson] = field(default_factory=dict)
    approval_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _id(self.request_id, "request_id", _REQUEST_ID))
        object.__setattr__(self, "project_id", _id(self.project_id, "project_id", _REQUEST_ID))
        object.__setattr__(self, "capability_id", _id(self.capability_id, "capability_id", _TOKEN))
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        object.__setattr__(self, "requested_by", _id(self.requested_by, "requested_by", _AGENT_ID))
        object.__setattr__(self, "inputs", _freeze(dict(self.inputs), "inputs"))
        object.__setattr__(self, "constraints", _freeze(dict(self.constraints), "constraints"))
        if self.approval_reference is not None:
            object.__setattr__(
                self,
                "approval_reference",
                _id(self.approval_reference, "approval_reference", _REQUEST_ID),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "capability_id": self.capability_id,
            "objective": self.objective,
            "requested_by": self.requested_by,
            "inputs": _thaw(self.inputs),
            "constraints": _thaw(self.constraints),
            "approval_reference": self.approval_reference,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRequest":
        return cls(
            request_id=data["request_id"],
            project_id=data["project_id"],
            capability_id=data["capability_id"],
            objective=data["objective"],
            requested_by=data["requested_by"],
            inputs=data.get("inputs", {}),
            constraints=data.get("constraints", {}),
            approval_reference=data.get("approval_reference"),
        )

    @classmethod
    def from_json(cls, payload: str) -> "AgentRequest":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise AgentContractError("agent request JSON must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    request_id: str
    agent_id: str
    status: AgentResponseStatus
    summary: str
    outputs: Mapping[str, FrozenJson] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    confidence: float = 0.0
    next_handoff: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _id(self.request_id, "request_id", _REQUEST_ID))
        object.__setattr__(self, "agent_id", _id(self.agent_id, "agent_id", _AGENT_ID))
        try:
            status = AgentResponseStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise AgentContractError("status is invalid") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(self, "outputs", _freeze(dict(self.outputs), "outputs"))
        object.__setattr__(self, "evidence", _texts(self.evidence, "evidence"))
        object.__setattr__(self, "warnings", _texts(self.warnings, "warnings"))
        object.__setattr__(self, "errors", _texts(self.errors, "errors"))

        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise AgentContractError("confidence must be numeric")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise AgentContractError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)

        if status is AgentResponseStatus.SUCCEEDED and self.errors:
            raise AgentContractError("a succeeded response cannot contain errors")
        if status is AgentResponseStatus.FAILED and not self.errors:
            raise AgentContractError("a failed response must contain an error")
        if status is AgentResponseStatus.BLOCKED and not (self.errors or self.warnings):
            raise AgentContractError("a blocked response must explain the block")
        if self.next_handoff is not None:
            object.__setattr__(self, "next_handoff", _id(self.next_handoff, "next_handoff", _AGENT_ID))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "summary": self.summary,
            "outputs": _thaw(self.outputs),
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "confidence": self.confidence,
            "next_handoff": self.next_handoff,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentResponse":
        return cls(
            request_id=data["request_id"],
            agent_id=data["agent_id"],
            status=AgentResponseStatus(data["status"]),
            summary=data["summary"],
            outputs=data.get("outputs", {}),
            evidence=tuple(data.get("evidence", ())),
            warnings=tuple(data.get("warnings", ())),
            errors=tuple(data.get("errors", ())),
            confidence=data.get("confidence", 0.0),
            next_handoff=data.get("next_handoff"),
        )

    @classmethod
    def from_json(cls, payload: str) -> "AgentResponse":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise AgentContractError("agent response JSON must be an object")
        return cls.from_dict(data)


class AgentRegistry:
    """Deterministic in-memory registry for immutable definitions."""

    def __init__(self, definitions: Iterable[AgentDefinition] = ()) -> None:
        self._definitions: dict[str, AgentDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._definitions

    def register(self, definition: AgentDefinition, *, replace: bool = False) -> None:
        if not isinstance(definition, AgentDefinition):
            raise AgentContractError("definition must be an AgentDefinition")
        if definition.agent_id in self._definitions and not replace:
            raise RegistryConflictError(f"agent already registered: {definition.agent_id}")
        self._definitions[definition.agent_id] = definition

    def unregister(self, agent_id: str) -> AgentDefinition:
        normalized = _id(agent_id, "agent_id", _AGENT_ID)
        try:
            return self._definitions.pop(normalized)
        except KeyError as exc:
            raise UnknownAgentError(normalized) from exc

    def get(self, agent_id: str) -> AgentDefinition:
        normalized = _id(agent_id, "agent_id", _AGENT_ID)
        try:
            return self._definitions[normalized]
        except KeyError as exc:
            raise UnknownAgentError(normalized) from exc

    def list(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def find_by_capability(self, capability_id: str) -> tuple[AgentDefinition, ...]:
        normalized = _id(capability_id, "capability_id", _TOKEN)
        return tuple(item for item in self.list() if item.supports(normalized))

    def resolve(
        self,
        capability_id: str,
        *,
        required_permissions: Iterable[str] = (),
        approval_reference: str | None = None,
    ) -> AgentDefinition:
        normalized = _id(capability_id, "capability_id", _TOKEN)
        required = set(_tokens(required_permissions, "required_permissions"))
        for definition in self.find_by_capability(normalized):
            if not required.issubset(definition.permissions):
                continue
            capability = definition.capability(normalized)
            if capability.requires_human_approval and not approval_reference:
                continue
            return definition
        raise CapabilityResolutionError(f"no agent can satisfy capability {normalized}")

    def to_dict(self) -> dict[str, Any]:
        return {"agents": [item.to_dict() for item in self.list()]}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRegistry":
        agents = data.get("agents")
        if not isinstance(agents, list):
            raise AgentContractError("registry agents must be a list")
        return cls(AgentDefinition.from_dict(item) for item in agents)

    @classmethod
    def from_json(cls, payload: str) -> "AgentRegistry":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise AgentContractError("registry JSON must be an object")
        return cls.from_dict(data)
