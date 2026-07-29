import tempfile
import unittest
from pathlib import Path

from elman_os.technology_policy import (
    TECHNOLOGY_STACK,
    audit_technology_policy,
    is_approved_frontend_source,
    is_approved_specialized_source,
    is_prohibited_source,
    validate_generated_paths,
)


class TechnologyPolicyTests(unittest.TestCase):
    def test_stack_is_python_first(self) -> None:
        self.assertEqual(TECHNOLOGY_STACK["core_language"], "Python >= 3.11")
        self.assertEqual(TECHNOLOGY_STACK["web_mobile_ui_default"], "Flet")
        self.assertIn("JavaScript/TypeScript", TECHNOLOGY_STACK["optional_frontend"])
        self.assertIn("Dart/Kotlin/Swift", TECHNOLOGY_STACK["optional_mobile"])
        self.assertIn("Rust/C/C++", TECHNOLOGY_STACK["optional_native"])
        self.assertIn("FastAPI", TECHNOLOGY_STACK["api"])

    def test_python_and_declarative_files_are_allowed(self) -> None:
        for path in (
            "src/app.py",
            "src/types.pyi",
            "README.md",
            "pyproject.toml",
            "contracts/openapi.yaml",
            "config/policy.json",
            "assets/logo.svg",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_prohibited_source(path))

    def test_javascript_and_typescript_are_allowed_only_in_frontend_areas(self) -> None:
        for path in (
            "apps/studio/frontend/main.ts",
            "apps/web/src/page.tsx",
            "apps/mobile/App.js",
            "templates/web/react/vite.config.ts",
            "generated/customer-app/src/app.jsx",
            r"apps\web\src\page.tsx",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_approved_frontend_source(path))
                self.assertFalse(is_prohibited_source(path))

        for path in (
            "src/elman_os/runtime.ts",
            "apps/control_api/server.js",
            "plugins/tool.ts",
            "tests/kernel.spec.ts",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_approved_frontend_source(path))
                self.assertTrue(is_prohibited_source(path))

    def test_specialized_languages_are_allowed_only_in_approved_layers(self) -> None:
        approved = (
            "apps/mobile/mobile.dart",
            "apps/mobile/Main.kt",
            "templates/mobile/App.swift",
            "extensions/native/engine.rs",
            "scripts/platform/windows/install.ps1",
            "infrastructure/deploy.sh",
            "migrations/001.sql",
        )
        for path in approved:
            with self.subTest(path=path):
                self.assertTrue(is_approved_specialized_source(path))
                self.assertFalse(is_prohibited_source(path))

        prohibited = (
            "src/elman_os/mobile.dart",
            "src/elman_os/Main.kt",
            "src/elman_os/engine.rs",
            "src/elman_os/install.ps1",
            "deploy.sh",
            "styles.css",
        )
        for path in prohibited:
            with self.subTest(path=path):
                self.assertFalse(is_approved_specialized_source(path))
                self.assertTrue(is_prohibited_source(path))

    def test_generated_path_validation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "politique technologique"):
            validate_generated_paths(["app/main.py", "src/elman_os/runtime.ts"])

    def test_generated_path_validation_accepts_bounded_frontend_sources(self) -> None:
        validate_generated_paths(
            [
                "app/main.py",
                "apps/web/src/page.tsx",
                "apps/web/src/styles.css",
                "apps/mobile/App.js",
                "apps/mobile/Main.kt",
                "extensions/native/engine.rs",
            ]
        )

    def test_audit_ignores_build_outputs_and_checks_source_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "apps" / "web").mkdir(parents=True)
            (root / "build").mkdir()
            (root / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
            (root / "src" / "app.ts").write_text("export {};", encoding="utf-8")
            (root / "apps" / "web" / "app.ts").write_text(
                "export {};", encoding="utf-8"
            )
            (root / "build" / "bundle.js").write_text("", encoding="utf-8")

            violations = audit_technology_policy(root)

        self.assertEqual(
            [item.path.as_posix() for item in violations],
            ["src/app.ts"],
        )

    def test_current_kit_contains_no_prohibited_sources(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_technology_policy(project_root), ())


if __name__ == "__main__":
    unittest.main()
