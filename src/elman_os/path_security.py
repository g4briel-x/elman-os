"""Fail-closed helpers for trusted operating-system path aliases."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Final


_MACOS_VAR_ALIAS: Final[Path] = Path("/var")
_MACOS_PRIVATE_DIRECTORY: Final[Path] = Path("/private")
_MACOS_PRIVATE_VAR: Final[Path] = Path("/private/var")
_MACOS_VAR_TARGETS: Final[frozenset[str]] = frozenset(
    {"private/var", "/private/var"}
)


def is_trusted_macos_var_alias(component: Path) -> bool:
    """Return whether *component* is the verified macOS ``/var`` alias.

    macOS exposes ``/var`` as an operating-system symlink to
    ``/private/var``. Only that exact alias is accepted. The link, its target,
    and the target parent must remain unchanged throughout verification. Any
    missing, substituted, nested, or user-controlled symlink fails closed.
    """

    candidate = Path(component)
    if sys.platform != "darwin" or candidate != _MACOS_VAR_ALIAS:
        return False

    try:
        alias_before = candidate.lstat()
        if not stat.S_ISLNK(alias_before.st_mode):
            return False
        if os.readlink(candidate) not in _MACOS_VAR_TARGETS:
            return False

        private_before = _MACOS_PRIVATE_DIRECTORY.lstat()
        if not stat.S_ISDIR(private_before.st_mode):
            return False
        target_before = _MACOS_PRIVATE_VAR.lstat()
        if not stat.S_ISDIR(target_before.st_mode):
            return False
        if candidate.resolve(strict=True) != _MACOS_PRIVATE_VAR:
            return False
        if not os.path.samestat(candidate.stat(), target_before):
            return False

        alias_after = candidate.lstat()
        private_after = _MACOS_PRIVATE_DIRECTORY.lstat()
        target_after = _MACOS_PRIVATE_VAR.lstat()
    except (OSError, RuntimeError):
        return False

    return (
        os.path.samestat(alias_before, alias_after)
        and os.path.samestat(private_before, private_after)
        and os.path.samestat(target_before, target_after)
    )
