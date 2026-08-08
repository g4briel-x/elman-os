import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import elman_os
from elman_os.cli import main as cli_main
from elman_os.release import DISPLAY_VERSION, PACKAGE_VERSION, validate_release


class ReleaseV070RC2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.manifest = json.loads(
            (cls.root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )

    def test_runtime_and_release_versions_align(self) -> None:
        self.assertEqual(elman_os.__version__, "0.7.0rc2")
        self.assertEqual(DISPLAY_VERSION, "0.7.0-rc.2")
        self.assertEqual(PACKAGE_VERSION, "0.7.0rc2")

    def test_package_metadata_declares_v070rc2(self) -> None:
        pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^version = "0\.7\.0rc2"$')

    def test_manifest_declares_complete_test_scope(self) -> None:
        self.assertEqual(self.manifest["version"], "0.7.0-rc.2")
        self.assertEqual(
            self.manifest["verification_scope"]["kernel_unittests"],
            1938,
        )
        self.assertEqual(
            self.manifest["verification_scope"][
                "macos_path_regressions_fixed"
            ],
            [
                "verified_system_var_alias",
                "nested_symlink_rejection_preserved",
                "symlink_persistence_root_rejection_preserved",
            ],
        )

    def test_manifest_declares_v07_boundaries(self) -> None:
        capabilities = set(self.manifest["included_capabilities"])
        self.assertTrue(
            {
                "immutable_multi_agent_contracts",
                "deterministic_execution_plans",
                "structured_project_memory",
                "fail_closed_final_verification",
                "read_only_studio_v07_oversight",
                "strict_checksum_inventory_coverage",
            }.issubset(capabilities)
        )

    def test_release_candidate_gates_remain_closed(self) -> None:
        scope = self.manifest["verification_scope"]
        self.assertTrue(self.manifest["release_candidate_validated"])
        self.assertFalse(self.manifest["final_release_approved"])
        self.assertTrue(self.manifest["not_production_ready"])
        self.assertFalse(scope["real_api_credentials_used"])
        self.assertFalse(scope["paid_api_calls"])

    def test_ci_execution_and_clean_install_are_recorded(self) -> None:
        scope = self.manifest["verification_scope"]
        matrix = self.manifest["verification_scope"]["ci_matrix_configured"]
        self.assertTrue(matrix["executed_in_this_bundle_build"])
        self.assertFalse(scope["multi_platform_ci_pending"])
        self.assertTrue(scope["clean_install_validation"])

    def test_migration_guide_is_present(self) -> None:
        migration = self.root / "MIGRATION-v0.7.0-rc.1-to-v0.7.0-rc.2.md"
        self.assertTrue(migration.is_file())
        text = migration.read_text(encoding="utf-8")
        self.assertIn("Retour arrière", text)
        self.assertIn("0.7.0rc2", text)
        self.assertIn("v0.7.0-rc.1", text)

    def test_changelog_starts_with_v070rc2(self) -> None:
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = re.findall(r"(?m)^## (v[^\n ]+)", changelog)
        self.assertTrue(releases)
        self.assertEqual(releases[0], "v0.7.0-rc.2")

    def test_readme_identifies_v070rc2(self) -> None:
        first_line = (self.root / "README.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "# ELMAN-OS Foundation Kit v0.7.0-rc.2")

    def test_archive_builder_targets_v070rc2(self) -> None:
        builder = (self.root / "scripts/build_release.py").read_text(encoding="utf-8")
        self.assertIn(
            'ARCHIVE_PREFIX = "elman-os-foundation-kit-v0.7.0-rc.2"',
            builder,
        )

    def test_archive_verifier_builds_twice(self) -> None:
        verifier = (self.root / "scripts/verify_release_installation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("second_archive", verifier)
        self.assertIn("ARCHIVE REPRODUCTIBLE", verifier)

    def test_workflow_covers_develop_and_main(self) -> None:
        workflow = (self.root / ".github/workflows/release-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("- develop/v0.7.0", workflow)
        self.assertIn("- main", workflow)

    def test_studio_oversight_is_routed_through_official_cli(self) -> None:
        with patch("elman_os.studio_v07.main", return_value=0) as studio_main:
            result = cli_main(
                [
                    "studio-oversight",
                    "--request",
                    "request.json",
                    "--report",
                    "report.json",
                    "--key-file",
                    "key.bin",
                    "--key-id",
                    "key:release-001",
                ]
            )
        self.assertEqual(result, 0)
        studio_main.assert_called_once_with(
            [
                "--request",
                "request.json",
                "--report",
                "report.json",
                "--key-file",
                "key.bin",
                "--key-id",
                "key:release-001",
            ]
        )

    def test_roadmap_marks_candidate_prepared(self) -> None:
        roadmap = (self.root / "docs/ROADMAP-v0.7.0.md").read_text(encoding="utf-8")
        self.assertIn("v0.7.0-rc.2", roadmap)
        self.assertIn("validation d’installation propre", roadmap)

    def test_current_release_passes(self) -> None:
        report = validate_release(self.root)
        self.assertTrue(report.ready, report.to_dict())


if __name__ == "__main__":
    unittest.main()
