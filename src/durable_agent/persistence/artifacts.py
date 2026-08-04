"""Atomic content-addressed local artifact storage."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.errors import (
    ArtifactIntegrityError,
    IdempotencyConflictError,
    NotFoundError,
    SecurityPolicyError,
)
from durable_agent.security.paths import open_readonly_no_follow, resolve_within_root

_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class LocalArtifactStore:
    """Store artifacts atomically beneath a private root using explicit IDs."""

    def __init__(self, root: Path, *, maximum_bytes: int = 50_000_000) -> None:
        self._root = root
        self._maximum_bytes = maximum_bytes
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = root.resolve(strict=True)

    async def put(self, artifact_id: str, content: bytes, *, media_type: str) -> str:
        del media_type
        self._validate_id(artifact_id)
        if len(content) > self._maximum_bytes:
            raise SecurityPolicyError("artifact exceeds configured size limit")
        target = resolve_within_root(self._root, artifact_id, must_exist=False)
        digest = sha256_digest(content)
        if target.exists():
            if sha256_digest(open_readonly_no_follow(target)) != digest:
                raise IdempotencyConflictError("artifact ID already stores different content")
            return digest
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=self._root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path = Path(temporary)
            temporary_path.chmod(0o600)
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                if sha256_digest(open_readonly_no_follow(target)) != digest:
                    raise IdempotencyConflictError(
                        "artifact ID concurrently stored different content"
                    ) from None
            temporary_path.unlink()
        except Exception:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()
            raise
        return digest

    async def get(self, artifact_id: str, *, expected_hash: str | None = None) -> bytes:
        """Read an artifact and optionally verify it against durable metadata."""
        self._validate_id(artifact_id)
        try:
            target = resolve_within_root(self._root, artifact_id)
        except SecurityPolicyError as exc:
            raise NotFoundError(f"artifact not found: {artifact_id}") from exc
        content = open_readonly_no_follow(target)
        if expected_hash is not None and sha256_digest(content) != expected_hash:
            raise ArtifactIntegrityError(f"artifact content hash mismatch: {artifact_id}")
        return content

    async def prune_orphans(
        self,
        *,
        known_artifact_ids: frozenset[str],
        older_than: datetime,
        dry_run: bool,
    ) -> tuple[str, ...]:
        """Remove old uncatalogued files while the caller holds maintenance exclusion."""
        if older_than.tzinfo is None:
            raise ValueError("artifact retention cutoff must be timezone-aware")
        candidates: list[str] = []
        for entry in sorted(self._root.iterdir(), key=lambda item: item.name):
            if entry.name == ".gitkeep" or entry.name in known_artifact_ids:
                continue
            metadata = entry.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise SecurityPolicyError(
                    f"refusing retention cleanup with symlink artifact: {entry.name}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if not (_ARTIFACT_ID.fullmatch(entry.name) or entry.name.startswith(".artifact-")):
                continue
            modified = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
            if modified >= older_than:
                continue
            candidates.append(entry.name)
            if not dry_run:
                # Re-check the inode class and age immediately before unlinking. The
                # maintenance caller is responsible for excluding concurrent writers.
                current = entry.lstat()
                if not stat.S_ISREG(current.st_mode):
                    raise SecurityPolicyError(
                        f"artifact changed type during retention cleanup: {entry.name}"
                    )
                current_modified = datetime.fromtimestamp(current.st_mtime, tz=timezone.utc)
                if current_modified < older_than:
                    entry.unlink()
        return tuple(candidates)

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise SecurityPolicyError("invalid artifact ID")
