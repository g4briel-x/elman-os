"""Deterministic routing plans for the ELMAN-OS production pipeline."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .catalog import get_agent


_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class ProjectKind(StrEnum):
    SAAS = "saas"
    MOBILE = "mobile"
    FULLSTACK = "fullstack"


@dataclass(frozen=True, slots=True)
class ProjectIntent:
    """Validated product request before any agent is routed."""

    name: str
    slug: str
    kind: ProjectKind
    platforms: tuple[str, ...]
    features: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Le nom du projet est obligatoire")
        if not _SLUG_PATTERN.fullmatch(self.slug):
            raise ValueError(
                "Le slug doit utiliser des minuscules, chiffres et tirets, "
                "et commencer par une lettre"
            )
        allowed_platforms = {"web", "android", "ios", "windows", "macos", "linux"}
        unknown = set(self.platforms) - allowed_platforms
        if unknown:
            raise ValueError(f"Plateformes inconnues: {', '.join(sorted(unknown))}")
        if self.kind == ProjectKind.SAAS and "web" not in self.platforms:
            raise ValueError("Un projet SaaS doit cibler la plateforme web")
        if self.kind == ProjectKind.MOBILE and not ({"android", "ios"} & set(self.platforms)):
            raise ValueError("Un projet mobile doit cibler Android ou iOS")
        if self.kind == ProjectKind.FULLSTACK:
            if "web" not in self.platforms or not ({"android", "ios"} & set(self.platforms)):
                raise ValueError(
                    "Un projet fullstack doit cibler le web et au moins Android ou iOS"
                )


@dataclass(frozen=True, slots=True)
class PlanStage:
    stage_id: str
    name: str
    agent_ids: tuple[str, ...]
    required_outputs: tuple[str, ...]
    human_gate_after: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    project: ProjectIntent
    stages: tuple[PlanStage, ...]
    metacognitive_agents: tuple[str, ...]
    final_verifier: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def production_agent_ids(self) -> tuple[str, ...]:
        return tuple(
            agent_id
            for stage in self.stages
            for agent_id in stage.agent_ids
            if agent_id not in {"ELMAN_NEXUS", "ELMAN_PROOF"}
        )


@dataclass(slots=True)
class PipelinePlanner:
    """Select the smallest professional team that covers the product intent."""

    def build(self, intent: ProjectIntent) -> ExecutionPlan:
        platforms = set(intent.platforms)
        feature_text = " ".join(intent.features).casefold()

        experience_agents = ["ELMAN_EXPERIENCE", "ELMAN_CANVAS", "ELMAN_INCLUSIVE"]
        implementation_agents = ["ELMAN_GATEWAY", "ELMAN_CORE", "ELMAN_DATA"]
        if "web" in platforms:
            implementation_agents.append("ELMAN_WEB")
        if {"android", "ios"} & platforms:
            implementation_agents.append("ELMAN_MOBILE")
        if any(
            marker in feature_text
            for marker in ("paiement", "payment", "notification", "webhook", "ia", "ai")
        ):
            implementation_agents.append("ELMAN_CONNECT")

        stages = (
            PlanStage(
                "frame",
                "Cadrage et contrat produit",
                ("ELMAN_NEXUS", "ELMAN_DISCOVERY"),
                (
                    "project.brief.json",
                    "product.spec.md",
                    "acceptance.matrix.json",
                ),
                human_gate_after=True,
            ),
            PlanStage(
                "experience",
                "Expérience et interface",
                tuple(experience_agents),
                ("ux.flows.md", "design.tokens.json", "accessibility.plan.md"),
                human_gate_after=True,
            ),
            PlanStage(
                "architecture",
                "Architecture, sécurité et contrats",
                ("ELMAN_ATLAS", "ELMAN_GATEWAY", "ELMAN_DATA", "ELMAN_SHIELD"),
                ("architecture.md", "openapi.yaml", "threat.model.md"),
                human_gate_after=True,
            ),
            PlanStage(
                "production",
                "Production bornée",
                tuple(dict.fromkeys(implementation_agents)),
                ("source", "tests", "evidence.json"),
            ),
            PlanStage(
                "integration",
                "Intégration, performance et documentation",
                ("ELMAN_VELOCITY", "ELMAN_FORGE", "ELMAN_SCRIBE"),
                ("build.report.json", "performance.baseline.json", "README.md"),
            ),
            PlanStage(
                "verification",
                "Vérification indépendante",
                ("ELMAN_PROOF",),
                ("proof.report.json", "release.verdict.json"),
                human_gate_after=True,
            ),
        )

        for stage in stages:
            for agent_id in stage.agent_ids:
                get_agent(agent_id)

        return ExecutionPlan(
            project=intent,
            stages=stages,
            metacognitive_agents=(
                "ELMAN_SUPERVISOR",
                "ELMAN_REFLECTIVE",
                "ELMAN_MEMORY",
                "ELMAN_LEARNING",
            ),
            final_verifier="ELMAN_PROOF",
        )

