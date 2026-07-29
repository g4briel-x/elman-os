import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from elman_os.plugins import (
    AcceptanceChecklistPlugin,
    BlueprintValidatorPlugin,
    PluginPermission,
    PluginManifest,
    PluginRegistry,
    ProjectInspectorPlugin,
    TechnologyAuditPlugin,
    ToolContext,
)


@dataclass(slots=True)
class DeployApprovalPlugin:
    manifest: PluginManifest = field(
        default_factory=lambda: PluginManifest(
            plugin_id="test.deploy",
            name="Test Deploy",
            version="1.0",
            description="Test only",
            permissions=frozenset({PluginPermission.DEPLOY}),
            human_approval_permissions=frozenset({PluginPermission.DEPLOY}),
        )
    )

    def invoke(
        self,
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        return {"ok": True}


class PluginTests(unittest.TestCase):
    def test_permission_is_required(self) -> None:
        registry = PluginRegistry()
        registry.register(ProjectInspectorPlugin())
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))
            with self.assertRaises(PermissionError):
                registry.invoke(
                    "elman.project_inspector",
                    "list",
                    {"path": "."},
                    context,
                )

    def test_path_traversal_is_rejected(self) -> None:
        registry = PluginRegistry()
        registry.register(ProjectInspectorPlugin())
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                workspace=Path(directory),
                approved_permissions={PluginPermission.READ_WORKSPACE},
            )
            with self.assertRaises(PermissionError):
                registry.invoke(
                    "elman.project_inspector",
                    "read_text",
                    {"path": "../outside.txt"},
                    context,
                )

    def test_acceptance_checklist(self) -> None:
        registry = PluginRegistry()
        registry.register(AcceptanceChecklistPlugin())
        with tempfile.TemporaryDirectory() as directory:
            result = registry.invoke(
                "elman.acceptance_checklist",
                "evaluate",
                {"expected": ["A", "B"], "validated": ["A"]},
                ToolContext(workspace=Path(directory)),
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing"], ["B"])

    def test_blueprint_validator(self) -> None:
        registry = PluginRegistry()
        registry.register(BlueprintValidatorPlugin())
        with tempfile.TemporaryDirectory() as directory:
            result = registry.invoke(
                "elman.blueprint_validator",
                "validate",
                {
                    "name": "Task SaaS",
                    "slug": "task-saas",
                    "kind": "saas",
                    "platforms": ["web"],
                },
                ToolContext(workspace=Path(directory)),
            )
        self.assertTrue(result["valid"])

    def test_technology_audit_detects_core_typescript(self) -> None:
        registry = PluginRegistry()
        registry.register(TechnologyAuditPlugin())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "elman_os").mkdir(parents=True)
            (root / "src" / "elman_os" / "bad.ts").write_text(
                "export {};",
                encoding="utf-8",
            )
            result = registry.invoke(
                "elman.technology_auditor",
                "audit",
                {"path": "."},
                ToolContext(
                    workspace=root,
                    approved_permissions={PluginPermission.READ_WORKSPACE},
                ),
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"][0]["path"], "src/elman_os/bad.ts")

    def test_technical_permission_does_not_replace_human_approval(self) -> None:
        registry = PluginRegistry()
        registry.register(DeployApprovalPlugin())
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                workspace=Path(directory),
                approved_permissions={PluginPermission.DEPLOY},
            )
            with self.assertRaisesRegex(PermissionError, "Approbation humaine"):
                registry.invoke("test.deploy", "run", {}, context)

            context.human_approved_permissions.add(PluginPermission.DEPLOY)
            self.assertEqual(
                registry.invoke("test.deploy", "run", {}, context),
                {"ok": True},
            )


if __name__ == "__main__":
    unittest.main()
