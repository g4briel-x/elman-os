"""Provider abstraction for model-backed agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import AgentOutput, AgentProfile, Evidence, TaskEnvelope


class AgentProvider(Protocol):
    async def run(self, agent: AgentProfile, task: TaskEnvelope) -> AgentOutput:
        """Execute one bounded agent task."""


@dataclass(slots=True)
class DeterministicDemoProvider:
    """Safe provider used for tests and local demonstrations.

    It does not call a language model and must not be mistaken for a production
    generation provider.
    """

    label: str = "deterministic-demo"

    async def run(self, agent: AgentProfile, task: TaskEnvelope) -> AgentOutput:
        return AgentOutput(
            agent_id=agent.agent_id,
            task_id=task.task_id,
            summary=f"{agent.name} a traité la tâche de démonstration.",
            evidence=[
                Evidence(
                    claim="Exécution déterministe terminée",
                    source=self.label,
                    observed=True,
                )
            ],
            confidence="high",
        )

