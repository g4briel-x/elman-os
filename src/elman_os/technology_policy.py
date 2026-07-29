"""Executable Python-first, layer-aware technology policy for ELMAN-OS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


TECHNOLOGY_STACK: dict[str, str] = {
    "core_language": "Python >= 3.11",
    "web_mobile_ui_default": "Flet",
    "optional_frontend": "JavaScript/TypeScript in approved web areas",
    "optional_mobile": "Dart/Kotlin/Swift/Java in approved mobile areas",
    "optional_native": "Rust/C/C++ in approved native-extension areas",
    "optional_platform_automation": "PowerShell/shell in approved platform areas",
    "api": "FastAPI + Pydantic",
    "persistence": "SQLite MVP; PostgreSQL adapter target",
    "tests": "unittest + pytest",
    "browser_tests": "Playwright Python API",
}


WEB_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".cjs",
        ".css",
        ".htm",
        ".html",
        ".js",
        ".jsx",
        ".less",
        ".mjs",
        ".sass",
        ".scss",
        ".svelte",
        ".ts",
        ".tsx",
        ".vue",
    }
)

MOBILE_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".dart",
        ".java",
        ".kt",
        ".kts",
        ".swift",
    }
)

NATIVE_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs"}
)

PLATFORM_SCRIPT_SUFFIXES: frozenset[str] = frozenset(
    {".bash", ".bat", ".cmd", ".fish", ".ps1", ".psm1", ".sh", ".zsh"}
)

DATA_SOURCE_SUFFIXES: frozenset[str] = frozenset({".sql"})

SPECIALIZED_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    WEB_SOURCE_SUFFIXES
    | MOBILE_SOURCE_SUFFIXES
    | NATIVE_SOURCE_SUFFIXES
    | PLATFORM_SCRIPT_SUFFIXES
    | DATA_SOURCE_SUFFIXES
)

# The kernel and its own tests stay Python-only. Specialized sources belong to
# product, template or platform layers and cannot be imported into this core.
PYTHON_ONLY_PREFIXES: tuple[str, ...] = (
    "src/elman_os",
    "tests",
    "apps/control_api",
    "plugins",
)

APPROVED_WEB_PREFIXES: tuple[str, ...] = (
    "apps/studio/frontend",
    "apps/web",
    "apps/mobile",
    "templates/web",
    "templates/mobile",
    "generated",
    "examples/frontends",
)

APPROVED_MOBILE_PREFIXES: tuple[str, ...] = (
    "apps/mobile",
    "templates/mobile",
    "generated",
)

APPROVED_NATIVE_PREFIXES: tuple[str, ...] = (
    "extensions/native",
    "generated",
)

APPROVED_PLATFORM_PREFIXES: tuple[str, ...] = (
    "infrastructure",
    "scripts/platform",
    "generated",
)

APPROVED_DATA_PREFIXES: tuple[str, ...] = (
    "data/sql",
    "migrations",
    "generated",
)

IGNORED_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


@dataclass(frozen=True, slots=True)
class TechnologyViolation:
    """A maintained source file that violates the technology policy."""

    path: PurePosixPath
    reason: str


def _portable_parts(path: str | Path) -> tuple[str, ...]:
    normalized = str(path).replace("\\", "/")
    return tuple(part.casefold() for part in PurePosixPath(normalized).parts)


def _has_prefix(path: str | Path, prefix: str) -> bool:
    parts = _portable_parts(path)
    prefix_parts = _portable_parts(prefix)
    return parts[: len(prefix_parts)] == prefix_parts


def is_approved_specialized_source(path: str | Path) -> bool:
    """Return whether a non-Python source belongs to an approved layer."""

    suffix = PurePosixPath(str(path).replace("\\", "/")).suffix.casefold()
    if any(_has_prefix(path, prefix) for prefix in PYTHON_ONLY_PREFIXES):
        return False
    boundary_map = (
        (WEB_SOURCE_SUFFIXES, APPROVED_WEB_PREFIXES),
        (MOBILE_SOURCE_SUFFIXES, APPROVED_MOBILE_PREFIXES),
        (NATIVE_SOURCE_SUFFIXES, APPROVED_NATIVE_PREFIXES),
        (PLATFORM_SCRIPT_SUFFIXES, APPROVED_PLATFORM_PREFIXES),
        (DATA_SOURCE_SUFFIXES, APPROVED_DATA_PREFIXES),
    )
    return any(
        suffix in suffixes
        and any(_has_prefix(path, prefix) for prefix in approved_prefixes)
        for suffixes, approved_prefixes in boundary_map
    )


def is_approved_frontend_source(path: str | Path) -> bool:
    """Backward-compatible frontend-specific check."""

    suffix = PurePosixPath(str(path).replace("\\", "/")).suffix.casefold()
    return suffix in WEB_SOURCE_SUFFIXES and is_approved_specialized_source(path)


def is_prohibited_source(path: str | Path) -> bool:
    """Return whether a maintained source path violates the stack policy."""

    suffix = PurePosixPath(str(path).replace("\\", "/")).suffix.casefold()
    return (
        suffix in SPECIALIZED_SOURCE_SUFFIXES
        and not is_approved_specialized_source(path)
    )


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts[:-1])


def _violation_reason(path: Path) -> str:
    suffix = path.suffix.casefold()
    return (
        f"source spécialisée {suffix} hors couche autorisée : "
        "le noyau ELMAN-OS reste en Python et les autres langages sont bornés "
        "aux couches web, mobile, native, données ou plateforme approuvées"
    )


def audit_technology_policy(root: str | Path) -> tuple[TechnologyViolation, ...]:
    """Audit maintained files below *root* and return deterministic violations."""

    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.exists():
        raise FileNotFoundError(resolved_root)
    if not resolved_root.is_dir():
        raise NotADirectoryError(resolved_root)

    violations: list[TechnologyViolation] = []
    for path in sorted(resolved_root.rglob("*")):
        if not path.is_file() or _is_ignored(path, resolved_root):
            continue
        relative_path = path.relative_to(resolved_root)
        if is_prohibited_source(relative_path):
            violations.append(
                TechnologyViolation(
                    path=PurePosixPath(relative_path.as_posix()),
                    reason=_violation_reason(relative_path),
                )
            )
    return tuple(violations)


def audit_python_only(root: str | Path) -> tuple[TechnologyViolation, ...]:
    """Backward-compatible alias for the former v0.2.1 command."""

    return audit_technology_policy(root)


def validate_generated_paths(paths: Iterable[str | Path]) -> None:
    """Reject generated paths that fall outside the bounded stack policy."""

    prohibited = sorted(
        {
            str(PurePosixPath(str(path).replace("\\", "/")))
            for path in paths
            if is_prohibited_source(path)
        }
    )
    if prohibited:
        labels = ", ".join(prohibited)
        raise ValueError(f"Sources hors politique technologique: {labels}")
