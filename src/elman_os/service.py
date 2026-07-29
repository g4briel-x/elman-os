"""Application service that composes planning, generation and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .generator import GenerationResult, StarterProjectGenerator
from .planning import ExecutionPlan, PipelinePlanner, ProjectIntent


@dataclass(slots=True)
class ElmanKernelService:
    planner: PipelinePlanner
    generator: StarterProjectGenerator

    @classmethod
    def default(cls) -> "ElmanKernelService":
        return cls(PipelinePlanner(), StarterProjectGenerator())

    def plan(self, intent: ProjectIntent) -> ExecutionPlan:
        return self.planner.build(intent)

    def generate(
        self,
        intent: ProjectIntent,
        output_directory: str | Path,
    ) -> GenerationResult:
        plan = self.plan(intent)
        return self.generator.generate(intent, plan, output_directory)

