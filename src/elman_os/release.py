"""Offline stable-release validation and integrity checks."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .technology_policy import audit_technology_policy

DISPLAY_VERSION = "0.6.0"
PACKAGE_VERSION = "0.6.0"
CHECKSUM_FILENAME = "RELEASE-CHECKSUMS.sha256"
_DIGEST_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
_RUNTIME_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "generated",
        ".elman",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        "node_modules",
        "htmlcov",
    }
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "credentials.json",
        "service-account.json",
    }
)
_SENSITIVE_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_REQUIRED_FILES = (
    "CHANGELOG.md",
    "MIGRATION-v0.3.1-to-v0.4.0.md",
    "MIGRATION-v0.4.0-to-v0.5.0.md",
    "MIGRATION-v0.5.0-to-v0.5.1.md",
    "MIGRATION-v0.5.1-to-v0.6.0-rc.1.md",
    "MIGRATION-v0.6.0-rc.1-to-v0.6.0-rc.2.md",
    "MIGRATION-v0.6.0-rc.2-to-v0.6.0.md",
    "README.md",
    "RELEASE-MANIFEST.json",
    "RELEASE-CHECKSUMS.sha256",
    "docs/RELEASE.md",
    ".github/workflows/release-validation.yml",
    "src/elman_os/__init__.py",
    "src/elman_os/release.py",
    "tests/test_release.py",
    "tests/test_release_v060.py",
    "scripts/verify_release_installation.py",
)


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    release: str
    python: str
    operating_system: str
    checks: tuple[ReleaseCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "release": self.release,
            "ready": self.ready,
            "python": self.python,
            "operating_system": self.operating_system,
            "checks": [asdict(check) for check in self.checks],
        }


class ReleaseIntegrityError(RuntimeError):
    """A release artifact is malformed, incomplete or has changed."""


def _excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(
        part in _IGNORED_DIRECTORY_NAMES or part.endswith(".egg-info")
        for part in relative.parts
    )


def iter_release_files(root: str | Path) -> tuple[Path, ...]:
    """Return deterministic regular files included in release checksums."""

    base = Path(root).resolve()
    files: list[Path] = []
    for path in base.rglob("*"):
        if _excluded(path, base) or path.name == CHECKSUM_FILENAME:
            continue
        if path.is_symlink():
            raise ReleaseIntegrityError("Les liens symboliques sont interdits")
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            files.append(path)
    return tuple(sorted(files, key=lambda item: item.relative_to(base).as_posix()))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_release_checksums(root: str | Path) -> Path:
    """Atomically write a deterministic checksum inventory."""

    base = Path(root).resolve()
    destination = base / CHECKSUM_FILENAME
    lines = [
        f"{sha256_file(path)}  {path.relative_to(base).as_posix()}"
        for path in iter_release_files(base)
    ]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".release-checksums-",
        dir=base,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return destination


def _checksum_entries(path: Path) -> tuple[tuple[str, PurePosixPath], ...]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseIntegrityError("L'inventaire SHA-256 est absent ou non régulier")
    entries: list[tuple[str, PurePosixPath]] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseIntegrityError("L'inventaire SHA-256 est illisible") from exc
    if not lines:
        raise ReleaseIntegrityError("L'inventaire SHA-256 est vide")
    for line in lines:
        match = _DIGEST_LINE.fullmatch(line)
        if match is None:
            raise ReleaseIntegrityError("Une ligne SHA-256 est invalide")
        raw_path = match.group(2)
        candidate = PurePosixPath(raw_path)
        if (
            candidate.is_absolute()
            or "\\" in raw_path
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ReleaseIntegrityError("Un chemin SHA-256 est non portable")
        normalized = candidate.as_posix()
        if normalized in seen:
            raise ReleaseIntegrityError("Un chemin SHA-256 est dupliqué")
        seen.add(normalized)
        entries.append((match.group(1), candidate))
    return tuple(entries)


def verify_release_checksums(root: str | Path) -> tuple[int, tuple[str, ...]]:
    """Verify the signed inventory boundary without following symlinks."""

    base = Path(root).resolve()
    failures: list[str] = []
    entries = _checksum_entries(base / CHECKSUM_FILENAME)
    for expected, relative in entries:
        candidate = base.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            failures.append(f"{relative.as_posix()}:missing")
            continue
        if base not in resolved.parents or not resolved.is_file() or candidate.is_symlink():
            failures.append(f"{relative.as_posix()}:unsafe")
            continue
        if not hashlib.sha256(resolved.read_bytes()).hexdigest() == expected:
            failures.append(f"{relative.as_posix()}:changed")
    return len(entries), tuple(failures)


def portable_name_failures(relative_paths: Iterable[str]) -> tuple[str, ...]:
    """Validate raw relative names before a host filesystem can normalize them."""

    failures: list[str] = []
    casefolded: dict[str, str] = {}
    for relative in sorted(relative_paths):
        if len(relative.encode("utf-8")) > 240:
            failures.append(f"{relative}:too_long")
        key = relative.casefold()
        previous = casefolded.get(key)
        if previous is not None and previous != relative:
            failures.append(f"{relative}:case_collision")
        casefolded[key] = relative
        if "\\" in relative:
            failures.append(f"{relative}:backslash")
        for part in relative.split("/"):
            stem = part.split(".", 1)[0].upper()
            if part.endswith((" ", ".")):
                failures.append(f"{relative}:trailing_character")
            if stem in _WINDOWS_RESERVED_NAMES:
                failures.append(f"{relative}:windows_reserved")
    return tuple(dict.fromkeys(failures))


def portable_path_failures(root: str | Path) -> tuple[str, ...]:
    """Detect path names that cannot safely round-trip across supported hosts."""

    base = Path(root).resolve()
    relative_paths = (
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if not _excluded(path, base)
    )
    return portable_name_failures(relative_paths)


def sensitive_file_failures(root: str | Path) -> tuple[str, ...]:
    base = Path(root).resolve()
    failures: list[str] = []
    for path in base.rglob("*"):
        if _excluded(path, base) or not path.is_file():
            continue
        lower_name = path.name.lower()
        if (
            lower_name in _SENSITIVE_FILENAMES
            or lower_name.endswith((".pem", ".p12", ".pfx"))
            or (lower_name.endswith(".key") and lower_name != "public.key")
        ):
            failures.append(path.relative_to(base).as_posix())
    return tuple(sorted(failures))


def sensitive_content_failures(root: str | Path) -> tuple[str, ...]:
    """Detect high-confidence credential markers without logging file content."""

    base = Path(root).resolve()
    failures: list[str] = []
    for path in iter_release_files(base):
        try:
            payload = path.read_bytes()
        except OSError:
            failures.append(f"{path.relative_to(base).as_posix()}:unreadable")
            continue
        if any(pattern.search(payload) for pattern in _SENSITIVE_CONTENT_PATTERNS):
            failures.append(f"{path.relative_to(base).as_posix()}:credential_marker")
    return tuple(sorted(failures))


def validate_release(
    root: str | Path,
    *,
    python_version: tuple[int, int] | None = None,
) -> ReleaseReport:
    """Run deterministic, offline release checks and return a safe report."""

    base = Path(root).resolve()
    version = python_version or (sys.version_info.major, sys.version_info.minor)
    checks: list[ReleaseCheck] = []

    required_missing = tuple(
        relative for relative in _REQUIRED_FILES if not (base / relative).is_file()
    )
    checks.append(
        ReleaseCheck(
            "required_files",
            not required_missing,
            "complete" if not required_missing else ",".join(required_missing),
        )
    )

    metadata_ok = False
    metadata_detail = "invalid"
    manifest: dict[str, object] = {}
    try:
        pyproject = tomllib.loads((base / "pyproject.toml").read_text("utf-8"))
        project = pyproject["project"]
        metadata_ok = (
            project["name"] == "elman-os-kernel"
            and project["version"] == PACKAGE_VERSION
            and project["requires-python"] == ">=3.11"
        )
        metadata_detail = "consistent" if metadata_ok else "mismatch"
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        metadata_ok = False
    checks.append(ReleaseCheck("package_metadata", metadata_ok, metadata_detail))

    runtime_ok = False
    try:
        init_text = (base / "src/elman_os/__init__.py").read_text("utf-8")
        match = _RUNTIME_VERSION.search(init_text)
        runtime_ok = match is not None and match.group(1) == PACKAGE_VERSION
    except (OSError, UnicodeError):
        runtime_ok = False
    checks.append(
        ReleaseCheck("runtime_version", runtime_ok, "consistent" if runtime_ok else "mismatch")
    )

    manifest_ok = False
    gates_ok = False
    try:
        manifest = json.loads((base / "RELEASE-MANIFEST.json").read_text("utf-8"))
        scope = manifest["verification_scope"]
        manifest_ok = (
            manifest["version"] == DISPLAY_VERSION
            and manifest["package_name"] == "elman-os-kernel"
            and scope["kernel_unittests"] == 278
        )
        gates_ok = (
            manifest["release_candidate_validated"] is True
            and manifest["final_release_approved"] is True
            and manifest["not_production_ready"] is True
            and scope["real_api_credentials_used"] is False
            and scope["paid_api_calls"] is False
            and scope["real_ai_provider_runtime"]
            == "adapter_present_not_network_validated"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        manifest_ok = False
        gates_ok = False
    checks.append(
        ReleaseCheck(
            "release_manifest",
            manifest_ok,
            "consistent" if manifest_ok else "mismatch",
        )
    )
    checks.append(
        ReleaseCheck(
            "production_gates",
            gates_ok,
            "closed" if gates_ok else "unsafe_or_undefined",
        )
    )

    supported = version >= (3, 11)
    checks.append(
        ReleaseCheck(
            "python_compatibility",
            supported,
            f"{version[0]}.{version[1]}",
        )
    )

    paths = portable_path_failures(base) if base.is_dir() else ("root:missing",)
    checks.append(
        ReleaseCheck(
            "portable_paths",
            not paths,
            "windows_macos_linux" if not paths else ",".join(paths[:5]),
        )
    )

    sensitive = (
        sensitive_file_failures(base) + sensitive_content_failures(base)
        if base.is_dir()
        else ("root",)
    )
    checks.append(
        ReleaseCheck(
            "sensitive_files",
            not sensitive,
            "none" if not sensitive else ",".join(sensitive[:5]),
        )
    )

    try:
        violations = audit_technology_policy(base)
    except (OSError, ValueError):
        violations = ("audit_failed",)
    checks.append(
        ReleaseCheck(
            "technology_policy",
            not violations,
            "pass" if not violations else f"{len(violations)} violation(s)",
        )
    )

    try:
        checked, checksum_failures = verify_release_checksums(base)
        checksum_detail = (
            f"{checked} files verified"
            if not checksum_failures
            else ",".join(checksum_failures[:5])
        )
        checksum_ok = not checksum_failures
    except ReleaseIntegrityError as exc:
        checksum_ok = False
        checksum_detail = str(exc)
    checks.append(ReleaseCheck("file_integrity", checksum_ok, checksum_detail))

    return ReleaseReport(
        release=DISPLAY_VERSION,
        python=platform.python_version(),
        operating_system=platform.system().lower(),
        checks=tuple(checks),
    )
