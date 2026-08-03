"""Verify the v0.6.0-rc.2 wheel and archive without network access."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

DISPLAY_VERSION = "0.6.0-rc.2"
PACKAGE_VERSION = "0.6.0rc2"
ARCHIVE_PREFIX = f"elman-os-foundation-kit-v{DISPLAY_VERSION}/"


def run(*arguments: str, cwd: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    subprocess.run(arguments, cwd=cwd, env=environment, check=True)


def environment_python(environment: Path) -> Path:
    return (
        environment / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else environment / "bin" / "python"
    )


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    with tempfile.TemporaryDirectory(prefix="elman-os-v060rc2-") as temporary:
        work = Path(temporary)
        wheels = work / "wheels"
        wheels.mkdir()

        run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheels),
            str(root),
            cwd=root,
        )
        wheel_files = tuple(
            wheels.glob(f"elman_os_kernel-{PACKAGE_VERSION}-*.whl")
        )
        if len(wheel_files) != 1:
            raise RuntimeError("Roue ELMAN-OS 0.6.0rc2 absente ou ambiguë")

        environment = work / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        isolated_python = environment_python(environment)
        run(
            str(isolated_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--force-reinstall",
            str(wheel_files[0]),
            cwd=work,
        )
        run(
            str(isolated_python),
            "-c",
            (
                "import elman_os; "
                f"assert elman_os.__version__ == '{PACKAGE_VERSION}'"
            ),
            cwd=work,
        )

        archive = work / f"ELMAN-OS-Foundation-Kit-v{DISPLAY_VERSION}.zip"
        run(
            sys.executable,
            str(root / "scripts" / "build_release.py"),
            "--output",
            str(archive),
            cwd=root,
        )
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            if not names or any(
                not name.startswith(ARCHIVE_PREFIX) for name in names
            ):
                raise RuntimeError("Préfixe d’archive RC2 invalide")
            required = {
                ARCHIVE_PREFIX + "pyproject.toml",
                ARCHIVE_PREFIX + "RELEASE-MANIFEST.json",
                ARCHIVE_PREFIX + "RELEASE-CHECKSUMS.sha256",
                ARCHIVE_PREFIX
                + "MIGRATION-v0.5.1-to-v0.6.0-rc.1.md",
                ARCHIVE_PREFIX
                + "MIGRATION-v0.6.0-rc.1-to-v0.6.0-rc.2.md",
            }
            if not required.issubset(names):
                raise RuntimeError("Archive v0.6.0-rc.2 incomplète")

    print("WHEEL 0.6.0rc2 INSTALLEE HORS RESEAU : PASS")
    print("ARCHIVE DETERMINISTE 0.6.0-rc.2 : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
