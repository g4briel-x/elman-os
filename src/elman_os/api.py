"""Optional FastAPI control plane for the ELMAN-OS kernel."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import AGENT_CATALOG
from .planning import ProjectIntent, ProjectKind
from .service import ElmanKernelService


def create_app(
    service: ElmanKernelService | None = None,
    *,
    generated_root: str | Path = "generated",
) -> Any:
    """Create the optional FastAPI app without making FastAPI a core dependency."""

    try:
        from fastapi import FastAPI
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError(
            'Installer le control plane avec: python -m pip install -e ".[api]"'
        ) from exc

    kernel = service or ElmanKernelService.default()
    output_root = Path(generated_root)
    app = FastAPI(title="ELMAN-OS Control API", version="0.3.1")

    class IntentPayload(BaseModel):
        name: str = Field(min_length=1, max_length=120)
        slug: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
        kind: ProjectKind
        platforms: list[str]
        features: list[str] = Field(default_factory=list)
        acceptance_criteria: list[str] = Field(default_factory=list)
        constraints: dict[str, Any] = Field(default_factory=dict)

        def to_intent(self) -> ProjectIntent:
            return ProjectIntent(
                name=self.name,
                slug=self.slug,
                kind=self.kind,
                platforms=tuple(self.platforms),
                features=tuple(self.features),
                acceptance_criteria=tuple(self.acceptance_criteria),
                constraints=dict(self.constraints),
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.3.1"}

    @app.get("/v1/agents")
    def agents() -> list[dict[str, Any]]:
        return [asdict(agent) for agent in AGENT_CATALOG]

    @app.post("/v1/plans")
    def plan(payload: IntentPayload) -> dict[str, Any]:
        return kernel.plan(payload.to_intent()).to_dict()

    @app.post("/v1/projects", status_code=201)
    def generate(payload: IntentPayload) -> dict[str, Any]:
        result = kernel.generate(payload.to_intent(), output_root)
        return {
            "project_root": str(result.project_root),
            "files": list(result.files),
            "plan": result.plan.to_dict(),
        }

    return app
