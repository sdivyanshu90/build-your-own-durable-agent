from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.domain.errors import SecurityPolicyError
from durable_agent.security.paths import (
    open_readonly_no_follow,
    reject_symlink,
    resolve_within_root,
    safe_relative_path,
)


def test_relative_and_absolute_paths_inside_root(tmp_path: Path) -> None:
    target = tmp_path / "folder" / "file.txt"
    target.parent.mkdir()
    target.write_text("safe")
    assert resolve_within_root(tmp_path, "folder/file.txt") == target
    assert resolve_within_root(tmp_path, target) == target


@pytest.mark.parametrize("candidate", ["../escape", "/etc/passwd"])
def test_traversal_and_absolute_escape_are_rejected(tmp_path: Path, candidate: str) -> None:
    with pytest.raises(SecurityPolicyError):
        resolve_within_root(tmp_path, candidate)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_text("secret")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityPolicyError, match="escapes"):
        resolve_within_root(tmp_path, "link/secret")


def test_new_file_resolves_existing_parent(tmp_path: Path) -> None:
    subdirectory = tmp_path / "safe"
    subdirectory.mkdir()
    assert (
        resolve_within_root(tmp_path, "safe/new.txt", must_exist=False) == subdirectory / "new.txt"
    )


def test_root_operation_is_rejected_by_default(tmp_path: Path) -> None:
    with pytest.raises(SecurityPolicyError, match="root itself"):
        resolve_within_root(tmp_path, ".")


def test_relative_read_and_direct_symlink_helpers(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("content")
    assert safe_relative_path(tmp_path, target) == "file.txt"
    assert open_readonly_no_follow(target) == b"content"
    reject_symlink(target)
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(SecurityPolicyError, match="symlink"):
        reject_symlink(link)
    with pytest.raises(SecurityPolicyError, match="open file safely"):
        open_readonly_no_follow(tmp_path / "missing")
    with pytest.raises(SecurityPolicyError, match="regular file"):
        open_readonly_no_follow(tmp_path)
