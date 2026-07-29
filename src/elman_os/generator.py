"""Safe deterministic project generator used by ELMAN-OS v0.3.1."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .planning import ExecutionPlan, ProjectIntent, ProjectKind
from .technology_policy import validate_generated_paths


def _python_package(slug: str) -> str:
    package = re.sub(r"[^a-z0-9]+", "_", slug.casefold()).strip("_")
    if not package or package[0].isdigit():
        raise ValueError("Le slug ne peut pas produire un nom de paquet Python valide")
    return package


@dataclass(frozen=True, slots=True)
class GenerationResult:
    project_root: Path
    files: tuple[str, ...]
    plan: ExecutionPlan


@dataclass(slots=True)
class WorkspaceSandbox:
    """Write only below one explicit project root."""

    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise PermissionError("Un chemin relatif non vide est obligatoire")
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PermissionError("Le chemin sort du workspace de génération")
        return candidate

    def write_text(self, relative_path: str, content: str) -> None:
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


@dataclass(slots=True)
class StarterProjectGenerator:
    """Generate an auditable Python starter without contacting an AI provider."""

    def generate(
        self,
        intent: ProjectIntent,
        plan: ExecutionPlan,
        output_directory: str | Path,
    ) -> GenerationResult:
        output_root = Path(output_directory).expanduser().resolve() / intent.slug
        if output_root.exists() and any(output_root.iterdir()):
            raise FileExistsError(
                f"Le projet existe déjà et ne sera pas écrasé: {output_root}"
            )
        sandbox = WorkspaceSandbox(output_root)
        package = _python_package(intent.slug)
        templates = self._templates(intent, plan, package)
        validate_generated_paths(templates)
        for relative_path, content in templates.items():
            sandbox.write_text(relative_path, content)
        return GenerationResult(
            project_root=output_root,
            files=tuple(sorted(templates)),
            plan=plan,
        )

    def _templates(
        self,
        intent: ProjectIntent,
        plan: ExecutionPlan,
        package: str,
    ) -> Mapping[str, str]:
        project_data = {
            "schema_version": "1.0",
            "generated_by": "ELMAN-OS v0.3.1 deterministic starter",
            "intent": asdict(intent),
            "pipeline": plan.to_dict(),
            "limitations": [
                "Aucun fournisseur de modèle IA n'a été appelé.",
                "Aucun déploiement ni service payant n'a été activé.",
                "Les dépendances optionnelles doivent être installées avant de lancer l'API ou Flet.",
            ],
        }
        files: dict[str, str] = {
            ".gitignore": "__pycache__/\n*.py[cod]\n.venv/\n*.db\n.env\n",
            "README.md": self._readme(intent, package),
            "elman.project.json": json.dumps(
                project_data, ensure_ascii=False, indent=2, default=str
            )
            + "\n",
            "pyproject.toml": self._pyproject(intent, package),
            f"src/{package}/__init__.py": (
                f'"""Application {intent.name} générée par ELMAN-OS."""\n\n'
                '__version__ = "0.1.0"\n'
            ),
            f"src/{package}/domain.py": self._domain_module(),
            f"src/{package}/api.py": self._api_module(package),
            "tests/test_domain.py": self._domain_test(package),
            "proof/acceptance.json": json.dumps(
                {
                    "criteria": list(intent.acceptance_criteria)
                    or [
                        "Le projet compile",
                        "Les tests de domaine passent",
                        "Aucun secret n'est inclus",
                    ],
                    "status": "pending",
                    "verifier": "ELMAN_PROOF",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        }
        if intent.kind in {ProjectKind.MOBILE, ProjectKind.FULLSTACK}:
            files[f"src/{package}/mobile.py"] = self._mobile_module(package)
        return files

    @staticmethod
    def _readme(intent: ProjectIntent, package: str) -> str:
        mobile_note = ""
        if intent.kind in {ProjectKind.MOBILE, ProjectKind.FULLSTACK}:
            mobile_note = (
                "\nInterface mobile optionnelle : installer `.[mobile]`, puis appeler "
                f"`{package}.mobile:main` avec Flet.\n"
            )
        return f"""# {intent.name}

Starter {intent.kind.value} généré localement par ELMAN-OS v0.3.1.

## Installation

```powershell
py -3.11 -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install --upgrade pip setuptools
.\\.venv\\Scripts\\python.exe -m pip install -e ".[api]"
.\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
.\\.venv\\Scripts\\python.exe -m uvicorn {package}.api:app --reload
```
{mobile_note}
## Limites

Ce starter est déterministe. Il doit encore recevoir les exigences, écrans,
intégrations et contrôles propres au produit avant toute mise en production.
"""

    @staticmethod
    def _pyproject(intent: ProjectIntent, package: str) -> str:
        return f"""[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "{intent.slug}"
version = "0.1.0"
description = "Starter {intent.kind.value} généré par ELMAN-OS"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
api = ["fastapi>=0.115,<1", "uvicorn>=0.30,<1"]
mobile = ["flet>=0.25,<1"]

[tool.setuptools]
package-dir = {{ "" = "src" }}

[tool.setuptools.packages.find]
where = ["src"]
include = ["{package}*"]
"""

    @staticmethod
    def _domain_module() -> str:
        return '''"""Domain model with a local SQLite repository."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    title: str
    completed: bool = False


class TaskRepository:
    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    def create(self, title: str) -> Task:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Le titre est obligatoire")
        task = Task(task_id=uuid4().hex, title=clean_title)
        self.connection.execute(
            "INSERT INTO tasks (task_id, title, completed) VALUES (?, ?, ?)",
            (task.task_id, task.title, int(task.completed)),
        )
        self.connection.commit()
        return task

    def list(self) -> list[Task]:
        rows = self.connection.execute(
            "SELECT task_id, title, completed FROM tasks ORDER BY rowid"
        ).fetchall()
        return [
            Task(row["task_id"], row["title"], bool(row["completed"]))
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TaskRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def serialize(task: Task) -> dict[str, object]:
        return asdict(task)
'''

    @staticmethod
    def _api_module(package: str) -> str:
        return f'''"""Optional FastAPI adapter for the generated application."""

from __future__ import annotations

from .domain import TaskRepository


repository = TaskRepository()

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise RuntimeError(
        'Installer les dépendances API avec: python -m pip install -e ".[api]"'
    ) from exc


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


app = FastAPI(title="{package}", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {{"status": "ok"}}


@app.get("/tasks")
def list_tasks() -> list[dict[str, object]]:
    return [repository.serialize(task) for task in repository.list()]


@app.post("/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict[str, object]:
    try:
        task = repository.create(payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return repository.serialize(task)
'''

    @staticmethod
    def _mobile_module(package: str) -> str:
        return f'''"""Optional Python/Flet mobile interface."""

from __future__ import annotations


def main(page: object) -> None:
    try:
        import flet as ft
    except ImportError as exc:
        raise RuntimeError(
            'Installer les dépendances mobiles avec: python -m pip install -e ".[mobile]"'
        ) from exc

    page.title = "{package}"
    page.add(ft.Text("Starter mobile ELMAN-OS"))
'''

    @staticmethod
    def _domain_test(package: str) -> str:
        return f'''import unittest

from {package}.domain import TaskRepository


class TaskRepositoryTests(unittest.TestCase):
    def test_create_and_list(self) -> None:
        with TaskRepository() as repository:
            created = repository.create("Première tâche")
            self.assertEqual(created.title, "Première tâche")
            self.assertEqual(repository.list(), [created])

    def test_empty_title_is_rejected(self) -> None:
        with TaskRepository() as repository:
            with self.assertRaises(ValueError):
                repository.create("   ")


if __name__ == "__main__":
    unittest.main()
'''
