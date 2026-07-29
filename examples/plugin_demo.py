"""Minimal demonstration of permissioned plugins."""

from pathlib import Path

from elman_os.plugins import (
    PluginPermission,
    ToolContext,
    built_in_registry,
)


def main() -> None:
    registry = built_in_registry()

    workspace = Path.cwd()
    read_context = ToolContext(
        workspace=workspace,
        approved_permissions={PluginPermission.READ_WORKSPACE},
    )
    pure_context = ToolContext(workspace=workspace)
    print(
        registry.invoke(
            "elman.project_inspector",
            "list",
            {"path": "src"},
            read_context,
        )
    )
    print(
        registry.invoke(
            "elman.blueprint_validator",
            "validate",
            {
                "name": "ELMAN Tasks",
                "slug": "elman-tasks",
                "kind": "saas",
                "platforms": ["web"],
            },
            pure_context,
        )
    )

    print(
        registry.invoke(
            "elman.acceptance_checklist",
            "evaluate",
            {
                "expected": ["AUTH-001", "AUTH-002"],
                "validated": ["AUTH-001"],
            },
            pure_context,
        )
    )


if __name__ == "__main__":
    main()
