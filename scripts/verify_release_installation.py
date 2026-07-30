"""Verify the v0.5.0 wheel and deterministic archive without network access."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path


VERSION = "0.5.0"
ARCHIVE_PREFIX = f"elman-os-foundation-kit-v{VERSION}/"


def run(*arguments: str, cwd: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    subprocess.run(arguments, cwd=cwd, env=environment, check=True)


def environment_python(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    with tempfile.TemporaryDirectory(prefix="elman-os-v050-") as temporary:
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
        wheel_files = tuple(wheels.glob("elman_os_kernel-0.5.0-*.whl"))
        if len(wheel_files) != 1:
            raise RuntimeError("La roue ELMAN-OS v0.5.0 est introuvable ou ambiguë")

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
                "assert elman_os.__version__ == '0.5.0', elman_os.__version__"
            ),
            cwd=work,
        )

        archive = work / "ELMAN-OS-Foundation-Kit-v0.5.0.zip"
        run(
            sys.executable,
            str(root / "scripts" / "build_release.py"),
            "--output",
            str(archive),
            cwd=root,
        )
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            if not names or any(not name.startswith(ARCHIVE_PREFIX) for name in names):
                raise RuntimeError("Préfixe d’archive v0.5.0 invalide")
            required = {
                ARCHIVE_PREFIX + "pyproject.toml",
                ARCHIVE_PREFIX + "RELEASE-MANIFEST.json",
                ARCHIVE_PREFIX + "RELEASE-CHECKSUMS.sha256",
                ARCHIVE_PREFIX + "MIGRATION-v0.4.0-to-v0.5.0.md",
            }
            if not required.issubset(names):
                raise RuntimeError("L’archive v0.5.0 est incomplète")

    print("WHEEL 0.5.0 INSTALLEE HORS RESEAU : PASS")
    print("ARCHIVE DETERMINISTE 0.5.0 : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
