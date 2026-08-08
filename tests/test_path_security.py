import os
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from elman_os import artifact_orchestration_selected_state_resume_persistence
from elman_os import artifact_orchestration_state_index
from elman_os import artifact_orchestration_state_persistence
from elman_os import artifact_orchestration_state_restoration
from elman_os.artifact_orchestration_selected_state_resume_persistence import (
    ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError,
)
from elman_os.artifact_orchestration_state_index import (
    ArtifactOrchestrationStateIndexIntegrityError as IndexIntegrityError,
)
from elman_os.artifact_orchestration_state_persistence import (
    ArtifactOrchestrationPersistenceIntegrityError as PersistenceIntegrityError,
)
from elman_os.artifact_orchestration_state_restoration import (
    ArtifactOrchestrationRestorationIntegrityError as RestorationIntegrityError,
)
from elman_os.path_security import is_trusted_macos_var_alias


ResumePersistenceIntegrityError = (
    ArtifactOrchestrationSelectedStateResumePersistenceIntegrityError
)


def _stat_result(mode: int, *, inode: int, device: int = 1) -> os.stat_result:
    return os.stat_result(
        (mode, inode, device, 1, 0, 0, 0, 0, 0, 0)
    )


class MacOSVarAliasTests(unittest.TestCase):
    alias_stat = _stat_result(stat.S_IFLNK | 0o777, inode=10)
    private_stat = _stat_result(stat.S_IFDIR | 0o755, inode=20)
    target_stat = _stat_result(stat.S_IFDIR | 0o755, inode=30)

    def _trusted_alias(self, link_target: str = "private/var"):
        def lstat(path: Path) -> os.stat_result:
            if path == Path("/var"):
                return self.alias_stat
            if path == Path("/private"):
                return self.private_stat
            if path == Path("/private/var"):
                return self.target_stat
            raise FileNotFoundError(path)

        def followed_stat(path: Path) -> os.stat_result:
            if path == Path("/var"):
                return self.target_stat
            raise FileNotFoundError(path)

        return (
            patch("elman_os.path_security.sys.platform", "darwin"),
            patch("elman_os.path_security.Path.lstat", lstat),
            patch("elman_os.path_security.Path.stat", followed_stat),
            patch("elman_os.path_security.os.readlink", return_value=link_target),
            patch(
                "elman_os.path_security.Path.resolve",
                return_value=Path("/private/var"),
            ),
        )

    def test_non_macos_platform_is_rejected_without_filesystem_access(self):
        with patch("elman_os.path_security.sys.platform", "linux"), patch(
            "elman_os.path_security.Path.lstat"
        ) as lstat:
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))
        lstat.assert_not_called()

    def test_non_var_component_is_rejected_without_filesystem_access(self):
        with patch("elman_os.path_security.sys.platform", "darwin"), patch(
            "elman_os.path_security.Path.lstat"
        ) as lstat:
            self.assertFalse(is_trusted_macos_var_alias(Path("/tmp/link")))
        lstat.assert_not_called()

    def test_relative_system_target_is_accepted(self):
        contexts = self._trusted_alias("private/var")
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            self.assertTrue(is_trusted_macos_var_alias(Path("/var")))

    def test_absolute_system_target_is_accepted(self):
        contexts = self._trusted_alias("/private/var")
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            self.assertTrue(is_trusted_macos_var_alias(Path("/var")))

    def test_unexpected_link_target_is_rejected(self):
        contexts = self._trusted_alias("private/attacker")
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_private_parent_symlink_is_rejected(self):
        contexts = self._trusted_alias()
        private_link = _stat_result(stat.S_IFLNK | 0o777, inode=20)
        with contexts[0], contexts[2], contexts[3], contexts[4], patch(
            "elman_os.path_security.Path.lstat",
            side_effect=[self.alias_stat, private_link],
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_private_var_symlink_is_rejected(self):
        contexts = self._trusted_alias()
        target_link = _stat_result(stat.S_IFLNK | 0o777, inode=30)
        with contexts[0], contexts[2], contexts[3], contexts[4], patch(
            "elman_os.path_security.Path.lstat",
            side_effect=[self.alias_stat, self.private_stat, target_link],
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_resolved_target_mismatch_is_rejected(self):
        contexts = self._trusted_alias()
        with contexts[0], contexts[1], contexts[2], contexts[3], patch(
            "elman_os.path_security.Path.resolve",
            return_value=Path("/private/attacker"),
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_target_inode_mismatch_is_rejected(self):
        contexts = self._trusted_alias()
        attacker_stat = _stat_result(stat.S_IFDIR | 0o755, inode=99)
        with contexts[0], contexts[1], contexts[3], contexts[4], patch(
            "elman_os.path_security.Path.stat",
            return_value=attacker_stat,
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_alias_replacement_during_verification_is_rejected(self):
        contexts = self._trusted_alias()
        replaced = _stat_result(stat.S_IFLNK | 0o777, inode=99)
        with contexts[0], contexts[2], contexts[3], contexts[4], patch(
            "elman_os.path_security.Path.lstat",
            side_effect=[
                self.alias_stat,
                self.private_stat,
                self.target_stat,
                replaced,
                self.private_stat,
                self.target_stat,
            ],
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    def test_filesystem_error_fails_closed(self):
        with patch("elman_os.path_security.sys.platform", "darwin"), patch(
            "elman_os.path_security.Path.lstat",
            side_effect=PermissionError("denied"),
        ):
            self.assertFalse(is_trusted_macos_var_alias(Path("/var")))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS")
    def test_live_macos_var_alias_is_trusted(self):
        self.assertTrue(is_trusted_macos_var_alias(Path("/var")))


class OrchestrationPathBoundaryTests(unittest.TestCase):
    boundaries = (
        (
            artifact_orchestration_state_index,
            IndexIntegrityError,
        ),
        (
            artifact_orchestration_state_persistence,
            PersistenceIntegrityError,
        ),
        (
            artifact_orchestration_state_restoration,
            RestorationIntegrityError,
        ),
        (
            artifact_orchestration_selected_state_resume_persistence,
            ResumePersistenceIntegrityError,
        ),
    )

    @staticmethod
    def _exists(path: Path) -> bool:
        return path.as_posix() in {"/", "/var", "/var/folders", "/var/folders/job"}

    @staticmethod
    def _var_is_symlink(path: Path) -> bool:
        return path == Path("/var")

    def test_all_boundaries_accept_only_the_trusted_var_component(self):
        with patch.object(Path, "exists", self._exists), patch.object(
            Path, "is_symlink", self._var_is_symlink
        ):
            for module, _ in self.boundaries:
                with self.subTest(module=module.__name__), patch.object(
                    module,
                    "is_trusted_macos_var_alias",
                    side_effect=lambda path: path == Path("/var"),
                ):
                    module._reject_symlink_components(
                        Path("/var/folders/job")
                    )

    def test_all_boundaries_reject_an_unverified_var_component(self):
        with patch.object(Path, "exists", self._exists), patch.object(
            Path, "is_symlink", self._var_is_symlink
        ):
            for module, error in self.boundaries:
                with self.subTest(module=module.__name__), patch.object(
                    module,
                    "is_trusted_macos_var_alias",
                    return_value=False,
                ):
                    with self.assertRaises(error):
                        module._reject_symlink_components(
                            Path("/var/folders/job")
                        )

    def test_all_boundaries_still_reject_nested_symlinks(self):
        def nested_is_symlink(path: Path) -> bool:
            return path in {Path("/var"), Path("/var/folders")}

        with patch.object(Path, "exists", self._exists), patch.object(
            Path, "is_symlink", nested_is_symlink
        ):
            for module, error in self.boundaries:
                with self.subTest(module=module.__name__), patch.object(
                    module,
                    "is_trusted_macos_var_alias",
                    side_effect=lambda path: path == Path("/var"),
                ):
                    with self.assertRaises(error):
                        module._reject_symlink_components(
                            Path("/var/folders/job")
                        )


if __name__ == "__main__":
    unittest.main()
