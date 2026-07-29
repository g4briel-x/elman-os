"""Permissioned plugin contracts and safe built-in examples."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .planning import ProjectIntent, ProjectKind
from .technology_policy import audit_technology_policy


class PluginPermission(StrEnum):
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    RUN_TESTS = "run_tests"
    NETWORK = "network"
    USE_SECRETS = "use_secrets"
    DEPLOY = "deploy"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    description: str
    permissions: frozenset[PluginPermission]
    human_approval_permissions: frozenset[PluginPermission] = frozenset()


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    approved_permissions: set[PluginPermission] = field(default_factory=set)
    human_approved_permissions: set[PluginPermission] = field(default_factory=set)

    def resolve_path(self, relative_path: str) -> Path:
        root = self.workspace.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise PermissionError("Le chemin sort du workspace autorisé")
        return candidate


class ElmanPlugin(Protocol):
    manifest: PluginManifest

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Execute one permission-checked plugin action."""


@dataclass(slots=True)
class PluginRegistry:
    _plugins: dict[str, ElmanPlugin] = field(default_factory=dict)

    def register(self, plugin: ElmanPlugin) -> None:
        plugin_id = plugin.manifest.plugin_id
        if plugin_id in self._plugins:
            raise ValueError(f"Plugin déjà enregistré: {plugin_id}")
        self._plugins[plugin_id] = plugin

    def invoke(
        self,
        plugin_id: str,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        try:
            plugin = self._plugins[plugin_id]
        except KeyError as exc:
            raise KeyError(f"Plugin inconnu: {plugin_id}") from exc

        missing = plugin.manifest.permissions - context.approved_permissions
        if missing:
            labels = ", ".join(sorted(permission.value for permission in missing))
            raise PermissionError(f"Permissions manquantes: {labels}")

        pending_human = (
            plugin.manifest.human_approval_permissions
            - context.human_approved_permissions
        )
        if pending_human:
            labels = ", ".join(
                sorted(permission.value for permission in pending_human)
            )
            raise PermissionError(f"Approbation humaine requise: {labels}")

        return plugin.invoke(action, arguments, context)

    @property
    def manifests(self) -> tuple[PluginManifest, ...]:
        return tuple(
            self._plugins[plugin_id].manifest for plugin_id in sorted(self._plugins)
        )


@dataclass(slots=True)
class ProjectInspectorPlugin:
    """Read-only plugin that refuses path traversal and oversized reads."""

    max_read_bytes: int = 1_000_000
    manifest: PluginManifest = field(
        default_factory=lambda: PluginManifest(
            plugin_id="elman.project_inspector",
            name="ELMAN Project Inspector",
            version="0.3.1",
            description="Liste et lit les fichiers texte d'un workspace approuvé.",
            permissions=frozenset({PluginPermission.READ_WORKSPACE}),
        )
    )

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        if action == "list":
            relative_path = str(arguments.get("path", "."))
            target = context.resolve_path(relative_path)
            if not target.is_dir():
                raise NotADirectoryError(relative_path)
            files = sorted(
                path.relative_to(context.workspace.resolve()).as_posix()
                for path in target.rglob("*")
                if path.is_file()
            )
            return {"files": files}

        if action == "read_text":
            relative_path = str(arguments["path"])
            target = context.resolve_path(relative_path)
            size = target.stat().st_size
            if size > self.max_read_bytes:
                raise ValueError("Fichier trop volumineux pour ce plugin")
            return {
                "path": relative_path,
                "content": target.read_text(encoding="utf-8"),
            }

        raise ValueError(f"Action non prise en charge: {action}")


@dataclass(slots=True)
class AcceptanceChecklistPlugin:
    """Pure plugin for deterministic acceptance-criteria accounting."""

    manifest: PluginManifest = field(
        default_factory=lambda: PluginManifest(
            plugin_id="elman.acceptance_checklist",
            name="ELMAN Acceptance Checklist",
            version="0.3.1",
            description="Compare les critères attendus aux identifiants validés.",
            permissions=frozenset(),
        )
    )

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        if action != "evaluate":
            raise ValueError(f"Action non prise en charge: {action}")
        expected = {str(item) for item in arguments.get("expected", [])}
        validated = {str(item) for item in arguments.get("validated", [])}
        missing = sorted(expected - validated)
        return {
            "passed": not missing,
            "validated": sorted(expected & validated),
            "missing": missing,
        }


@dataclass(slots=True)
class TechnologyAuditPlugin:
    """Read-only enforcement of the Python-first layer boundaries."""

    manifest: PluginManifest = field(
        default_factory=lambda: PluginManifest(
            plugin_id="elman.technology_auditor",
            name="ELMAN Technology Auditor",
            version="0.3.1",
            description="Audite les langages et leurs couches autorisées.",
            permissions=frozenset({PluginPermission.READ_WORKSPACE}),
        )
    )

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        if action != "audit":
            raise ValueError(f"Action non prise en charge: {action}")
        target = context.resolve_path(str(arguments.get("path", ".")))
        violations = audit_technology_policy(target)
        return {
            "passed": not violations,
            "violations": [
                {"path": item.path.as_posix(), "reason": item.reason}
                for item in violations
            ],
        }


@dataclass(slots=True)
class BlueprintValidatorPlugin:
    """Pure validation plugin for an application intent."""

    manifest: PluginManifest = field(
        default_factory=lambda: PluginManifest(
            plugin_id="elman.blueprint_validator",
            name="ELMAN Blueprint Validator",
            version="0.3.1",
            description="Valide le type, le slug, les plateformes et les critères.",
            permissions=frozenset(),
        )
    )

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        if action != "validate":
            raise ValueError(f"Action non prise en charge: {action}")
        intent = ProjectIntent(
            name=str(arguments.get("name", "")),
            slug=str(arguments.get("slug", "")),
            kind=ProjectKind(str(arguments.get("kind", ""))),
            platforms=tuple(str(item) for item in arguments.get("platforms", [])),
            features=tuple(str(item) for item in arguments.get("features", [])),
            acceptance_criteria=tuple(
                str(item) for item in arguments.get("acceptance_criteria", [])
            ),
        )
        return {
            "valid": True,
            "name": intent.name,
            "slug": intent.slug,
            "kind": intent.kind.value,
            "platforms": list(intent.platforms),
        }


def built_in_registry() -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in (
        ProjectInspectorPlugin(),
        AcceptanceChecklistPlugin(),
        TechnologyAuditPlugin(),
        BlueprintValidatorPlugin(),
    ):
        registry.register(plugin)
    return registry
