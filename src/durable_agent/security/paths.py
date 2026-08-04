"""Filesystem confinement helpers resistant to traversal and symlink escape."""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

from durable_agent.domain.errors import SecurityPolicyError


def resolve_within_root(
    root: Path,
    candidate: str | Path,
    *,
    must_exist: bool = True,
    allow_root: bool = False,
) -> Path:
    """Resolve a path and prove it is beneath root.

    Existing parents are resolved so a symlink cannot redirect a new file outside root.
    """
    root_resolved = root.resolve(strict=True)
    raw = Path(candidate)
    combined = raw if raw.is_absolute() else root_resolved / raw
    try:
        if must_exist:
            resolved = combined.resolve(strict=True)
        else:
            resolved_parent = combined.parent.resolve(strict=True)
            resolved = resolved_parent / combined.name
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise SecurityPolicyError(
            f"path cannot be safely resolved: {candidate}", details={"path": str(candidate)}
        ) from exc
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SecurityPolicyError(
            f"path escapes approved root: {candidate}",
            details={"root": str(root_resolved), "resolved": str(resolved)},
        ) from exc
    if not allow_root and relative == Path():
        raise SecurityPolicyError("operation on repository root itself is not allowed")
    return resolved


def safe_relative_path(root: Path, path: Path) -> str:
    """Return a normalized POSIX relative path after confinement validation."""
    resolved = resolve_within_root(root, path, allow_root=False)
    return resolved.relative_to(root.resolve(strict=True)).as_posix()


def reject_symlink(path: Path) -> None:
    """Reject a direct symlink using lstat semantics."""
    try:
        if path.is_symlink():
            raise SecurityPolicyError(f"symlink is not allowed: {path}")
    except OSError as exc:
        raise SecurityPolicyError(f"cannot inspect path safely: {path}") from exc


def open_readonly_no_follow(path: Path) -> bytes:
    """Read a regular file without following a final symlink when supported."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SecurityPolicyError(f"unable to open file safely: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat_module.S_ISREG(file_stat.st_mode) or file_stat.st_nlink < 1:
            raise SecurityPolicyError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
