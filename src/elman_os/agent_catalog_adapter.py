"""Deterministic adapter from the legacy agent catalog to v0.7 contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .agent_contracts import (
    AgentCapability,
    AgentContractError,
    AgentDefinition,
    AgentRegistry,
)
from .catalog import AGENT_CATALOG
from .domain import AgentLayer, AgentProfile


CATALOG_CONTRACT_VERSION = "1.0.0"
_OUTPUT_KIND = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def _contract_output_kind(value: object) -> str:
    """Map a legacy artifact name to the strict lowercase token contract.

    The original value remains unchanged in AgentDefinition.metadata.
    Only ASCII case is normalized; unsupported characters remain rejected.
    """

    original = _required_text(value, "profile.required_outputs")
    adapted = original.lower()

    if _OUTPUT_KIND.fullmatch(adapted) is None:
        raise AgentContractError(
            "profile.required_outputs contains a value that cannot be adapted "
            "to a strict output kind"
        )

    return adapted


def profile_to_definition(
    profile: AgentProfile,
    *,
    version: str = CATALOG_CONTRACT_VERSION,
) -> AgentDefinition:
    """Convert one legacy AgentProfile into one strict v0.7 definition.

    Conversion is fail-closed: invalid identifiers, scopes, outputs,
    permissions, layers or versions are rejected by the strict contracts.
    """

    if not isinstance(profile, AgentProfile):
        raise AgentContractError("profile must be an AgentProfile")
    if not isinstance(profile.layer, AgentLayer):
        raise AgentContractError("profile.layer must be an AgentLayer")

    mission = _required_text(profile.mission, "profile.mission")
    experience_standard = _required_text(
        profile.experience_standard,
        "profile.experience_standard",
    )

    scopes = tuple(profile.allowed_scopes)
    if not scopes:
        raise AgentContractError(
            f"{profile.agent_id} must expose at least one allowed scope"
        )

    required_outputs = tuple(profile.required_outputs)
    if not required_outputs:
        raise AgentContractError(
            f"{profile.agent_id} must declare at least one required output"
        )

    contract_output_kinds = tuple(
        _contract_output_kind(output)
        for output in required_outputs
    )

    capabilities = tuple(
        AgentCapability(
            capability_id=scope,
            description=(
                f"Legacy scope {scope} adapted from {profile.agent_id}"
            ),
            output_kinds=contract_output_kinds,
            permissions=(scope,),
            requires_human_approval=False,
        )
        for scope in scopes
    )

    return AgentDefinition(
        agent_id=profile.agent_id,
        name=profile.name,
        role=profile.role,
        version=version,
        capabilities=capabilities,
        permissions=scopes,
        forbidden_actions=tuple(profile.forbidden_actions),
        fail_closed=True,
        metadata={
            "adapter": "legacy-agent-profile-v1",
            "experience_standard": experience_standard,
            "layer": profile.layer.value,
            "mission": mission,
            "required_outputs": list(required_outputs),
        },
    )


def catalog_to_definitions(
    profiles: Iterable[AgentProfile],
    *,
    version: str = CATALOG_CONTRACT_VERSION,
) -> tuple[AgentDefinition, ...]:
    """Convert a catalog into definitions ordered by stable agent identifier."""

    if isinstance(profiles, (str, bytes)):
        raise AgentContractError("profiles must be an iterable of AgentProfile")

    definitions = tuple(
        profile_to_definition(profile, version=version)
        for profile in profiles
    )
    return tuple(sorted(definitions, key=lambda item: item.agent_id))


def catalog_to_registry(
    profiles: Iterable[AgentProfile],
    *,
    version: str = CATALOG_CONTRACT_VERSION,
) -> AgentRegistry:
    """Build a fresh deterministic registry from legacy profiles."""

    return AgentRegistry(
        catalog_to_definitions(profiles, version=version)
    )


def built_in_agent_registry(
    *,
    version: str = CATALOG_CONTRACT_VERSION,
) -> AgentRegistry:
    """Return a fresh registry containing the canonical 21-agent roster."""

    return catalog_to_registry(AGENT_CATALOG, version=version)
