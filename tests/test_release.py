import json
import shutil
import tempfile
import unittest
from pathlib import Path

from elman_os.release import (
    CHECKSUM_FILENAME,
    DISPLAY_VERSION,
    ReleaseIntegrityError,
    portable_name_failures,
    portable_path_failures,
    sensitive_content_failures,
    sensitive_file_failures,
    validate_release,
    verify_release_checksums,
    write_release_checksums,
)


class ChecksumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src/example.py").write_text("value = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generated_workspace_is_excluded_from_release_inventory(self) -> None:
        generated = self.root / "generated" / "demo"
        generated.mkdir(parents=True)
        (generated / "artifact.txt").write_text(
            "generated artifact\n",
            encoding="utf-8",
        )

        inventory = write_release_checksums(self.root)
        content = inventory.read_text(encoding="utf-8")

        self.assertIn("src/example.py", content)
        self.assertNotIn("generated/demo/artifact.txt", content)

    def test_checksum_inventory_is_sorted_and_verifiable(self) -> None:
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        inventory = write_release_checksums(self.root)
        lines = inventory.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, sorted(lines, key=lambda line: line[66:]))
        self.assertEqual(verify_release_checksums(self.root), (2, ()))

    def test_changed_file_is_detected(self) -> None:
        write_release_checksums(self.root)
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        self.assertEqual(
            verify_release_checksums(self.root)[1],
            ("src/example.py:changed",),
        )

    def test_missing_file_is_detected(self) -> None:
        write_release_checksums(self.root)
        (self.root / "src/example.py").unlink()
        self.assertEqual(
            verify_release_checksums(self.root)[1],
            ("src/example.py:missing",),
        )

    def test_malformed_checksum_line_is_rejected(self) -> None:
        (self.root / CHECKSUM_FILENAME).write_text("invalid\n", encoding="utf-8")
        with self.assertRaises(ReleaseIntegrityError):
            verify_release_checksums(self.root)

    def test_checksum_path_traversal_is_rejected(self) -> None:
        (self.root / CHECKSUM_FILENAME).write_text(
            f"{'0' * 64}  ../outside\n",
            encoding="utf-8",
        )
        with self.assertRaises(ReleaseIntegrityError):
            verify_release_checksums(self.root)

    def test_absolute_checksum_path_is_rejected(self) -> None:
        (self.root / CHECKSUM_FILENAME).write_text(
            f"{'0' * 64}  /outside\n",
            encoding="utf-8",
        )
        with self.assertRaises(ReleaseIntegrityError):
            verify_release_checksums(self.root)

    def test_duplicate_checksum_path_is_rejected(self) -> None:
        line = f"{'0' * 64}  src/example.py\n"
        (self.root / CHECKSUM_FILENAME).write_text(line + line, encoding="utf-8")
        with self.assertRaises(ReleaseIntegrityError):
            verify_release_checksums(self.root)

    def test_uppercase_digest_is_rejected(self) -> None:
        (self.root / CHECKSUM_FILENAME).write_text(
            f"{'A' * 64}  src/example.py\n",
            encoding="utf-8",
        )
        with self.assertRaises(ReleaseIntegrityError):
            verify_release_checksums(self.root)


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_safe_paths_are_accepted(self) -> None:
        (self.root / "safe-name.txt").write_text("safe", encoding="utf-8")
        self.assertEqual(portable_path_failures(self.root), ())

    def test_windows_reserved_name_is_rejected(self) -> None:
        self.assertIn(
            "CON.txt:windows_reserved",
            portable_name_failures(("CON.txt",)),
        )

    def test_trailing_dot_is_rejected(self) -> None:
        self.assertIn(
            "unsafe.:trailing_character",
            portable_name_failures(("unsafe.",)),
        )

    def test_case_collision_is_rejected(self) -> None:
        self.assertTrue(
            any(
                item.endswith(":case_collision")
                for item in portable_name_failures(("Readme", "README"))
            )
        )

    def test_backslash_name_is_rejected(self) -> None:
        self.assertIn(
            "bad\\name.txt:backslash",
            portable_name_failures(("bad\\name.txt",)),
        )

    def test_sensitive_file_is_detected(self) -> None:
        (self.root / ".env").write_text("SECRET=fake", encoding="utf-8")
        (self.root / "normal.txt").write_text(
            "-----BEGIN " + "PRIVATE KEY-----\nfake\n",
            encoding="utf-8",
        )
        self.assertEqual(sensitive_file_failures(self.root), (".env",))
        self.assertEqual(
            sensitive_content_failures(self.root),
            ("normal.txt:credential_marker",),
        )


class StableReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release_root = Path(__file__).resolve().parents[1]

    def copied_release(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        destination = Path(temporary.name) / "release"
        shutil.copytree(
            self.release_root,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
        return temporary, destination

    def test_current_release_passes_all_checks(self) -> None:
        report = validate_release(self.release_root)
        self.assertTrue(report.ready, report.to_dict())

    def test_report_is_json_serializable(self) -> None:
        encoded = json.dumps(validate_release(self.release_root).to_dict())
        self.assertIn(DISPLAY_VERSION, encoded)

    def test_missing_required_file_fails_closed(self) -> None:
        temporary, release = self.copied_release()
        try:
            (release / "docs/RELEASE.md").unlink()
            report = validate_release(release)
        finally:
            temporary.cleanup()
        self.assertFalse(report.ready)
        self.assertFalse(next(item for item in report.checks if item.name == "required_files").passed)

    def test_metadata_version_mismatch_fails_closed(self) -> None:
        temporary, release = self.copied_release()
        try:
            path = release / "pyproject.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace("0.5.1", "0.5.2"),
                encoding="utf-8",
            )
            report = validate_release(release)
        finally:
            temporary.cleanup()
        self.assertFalse(next(item for item in report.checks if item.name == "package_metadata").passed)

    def test_production_gate_change_fails_closed(self) -> None:
        temporary, release = self.copied_release()
        try:
            path = release / "RELEASE-MANIFEST.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["not_production_ready"] = False
            path.write_text(json.dumps(manifest), encoding="utf-8")
            report = validate_release(release)
        finally:
            temporary.cleanup()
        self.assertFalse(next(item for item in report.checks if item.name == "production_gates").passed)

    def test_unsupported_python_fails_closed(self) -> None:
        report = validate_release(self.release_root, python_version=(3, 10))
        check = next(item for item in report.checks if item.name == "python_compatibility")
        self.assertFalse(check.passed)


if __name__ == "__main__":
    unittest.main()
