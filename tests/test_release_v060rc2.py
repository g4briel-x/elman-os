import json
import re
import unittest
from pathlib import Path

import elman_os
from elman_os.release import DISPLAY_VERSION, PACKAGE_VERSION, validate_release


class ReleaseV060RC2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.manifest = json.loads(
            (cls.root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )

    def test_runtime_and_release_versions_align(self) -> None:
        self.assertEqual(elman_os.__version__, "0.6.0rc2")
        self.assertEqual(DISPLAY_VERSION, "0.6.0-rc.2")
        self.assertEqual(PACKAGE_VERSION, "0.6.0rc2")

    def test_package_metadata_declares_v060rc2(self) -> None:
        pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^version = "0\.6\.0rc2"$')

    def test_manifest_declares_278_tests(self) -> None:
        self.assertEqual(self.manifest["version"], "0.6.0-rc.2")
        self.assertEqual(
            self.manifest["verification_scope"]["kernel_unittests"], 278
        )

    def test_manifest_declares_rc2_hardening(self) -> None:
        capabilities = set(self.manifest["included_capabilities"])
        self.assertTrue(
            {
                "release_inventory_tooling_exclusions",
                "transaction_exit_contract_hardening",
                "v060rc1_to_v060rc2_migration_validation",
                "deterministic_v060rc2_archive",
            }.issubset(capabilities)
        )

    def test_release_candidate_gates_remain_closed(self) -> None:
        scope = self.manifest["verification_scope"]
        self.assertTrue(self.manifest["release_candidate_validated"])
        self.assertFalse(self.manifest["final_release_approved"])
        self.assertTrue(self.manifest["not_production_ready"])
        self.assertFalse(scope["real_api_credentials_used"])
        self.assertFalse(scope["paid_api_calls"])

    def test_migration_guide_is_present(self) -> None:
        migration = self.root / "MIGRATION-v0.6.0-rc.1-to-v0.6.0-rc.2.md"
        self.assertTrue(migration.is_file())
        text = migration.read_text(encoding="utf-8")
        self.assertIn("Retour arrière", text)
        self.assertIn("0.6.0rc2", text)
        self.assertIn("v0.6.0-rc.1", text)

    def test_changelog_starts_with_v060rc2(self) -> None:
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = re.findall(r"(?m)^## (v[^\n ]+)", changelog)
        self.assertTrue(releases)
        self.assertEqual(releases[0], "v0.6.0-rc.2")

    def test_readme_identifies_v060rc2(self) -> None:
        first_line = (
            (self.root / "README.md").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(
            first_line, "# ELMAN-OS Foundation Kit v0.6.0-rc.2"
        )

    def test_archive_builder_targets_v060rc2(self) -> None:
        builder = (self.root / "scripts/build_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'ARCHIVE_PREFIX = "elman-os-foundation-kit-v0.6.0-rc.2"',
            builder,
        )

    def test_current_release_passes(self) -> None:
        report = validate_release(self.root)
        self.assertTrue(report.ready, report.to_dict())


if __name__ == "__main__":
    unittest.main()
