"""Build a deterministic ELMAN-OS release ZIP with SHA-256 inventory."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from elman_os.release import iter_release_files, write_release_checksums  # noqa: E402

FIXED_TIMESTAMP = (2026, 8, 1, 0, 0, 0)
ARCHIVE_PREFIX = "elman-os-foundation-kit-v0.5.1"


def build_archive(root: Path, output: Path) -> None:
    write_release_checksums(root)
    files = list(iter_release_files(root)) + [root / "RELEASE-CHECKSUMS.sha256"]
    prefix = ARCHIVE_PREFIX
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(f"{prefix}/{relative}", FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT.parent / "ELMAN-OS-Foundation-Kit-v0.5.1.zip",
    )
    args = parser.parse_args()
    build_archive(ROOT, args.output.resolve())
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
