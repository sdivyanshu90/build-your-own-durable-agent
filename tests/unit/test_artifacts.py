from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from durable_agent.domain.errors import (
    ArtifactIntegrityError,
    IdempotencyConflictError,
    SecurityPolicyError,
)
from durable_agent.persistence.artifacts import LocalArtifactStore


@pytest.mark.asyncio
async def test_artifact_round_trip_and_idempotency(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    digest = await store.put("report.md", b"content", media_type="text/markdown")
    assert await store.get("report.md") == b"content"
    assert await store.put("report.md", b"content", media_type="text/plain") == digest
    with pytest.raises(IdempotencyConflictError):
        await store.put("report.md", b"different", media_type="text/plain")


@pytest.mark.asyncio
async def test_artifact_path_and_size_boundaries(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", maximum_bytes=3)
    with pytest.raises(SecurityPolicyError, match="ID"):
        await store.put("../escape", b"x", media_type="text/plain")
    with pytest.raises(SecurityPolicyError, match="size"):
        await store.put("large", b"1234", media_type="text/plain")


@pytest.mark.asyncio
async def test_artifact_tampering_is_detected_against_catalog_hash(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    digest = await store.put("result.json", b"original", media_type="application/json")
    assert await store.get("result.json", expected_hash=digest) == b"original"
    (root / "result.json").write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        await store.get("result.json", expected_hash=digest)


@pytest.mark.asyncio
async def test_retention_prunes_only_old_uncatalogued_regular_files(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    await store.put("known.txt", b"known", media_type="text/plain")
    await store.put("orphan.txt", b"orphan", media_type="text/plain")
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_timestamp = (cutoff - timedelta(days=1)).timestamp()
    os.utime(root / "known.txt", (old_timestamp, old_timestamp))
    os.utime(root / "orphan.txt", (old_timestamp, old_timestamp))

    preview = await store.prune_orphans(
        known_artifact_ids=frozenset({"known.txt"}),
        older_than=cutoff,
        dry_run=True,
    )
    assert preview == ("orphan.txt",)
    assert (root / "orphan.txt").exists()
    deleted = await store.prune_orphans(
        known_artifact_ids=frozenset({"known.txt"}),
        older_than=cutoff,
        dry_run=False,
    )
    assert deleted == preview
    assert (root / "known.txt").is_file()
    assert not (root / "orphan.txt").exists()


@pytest.mark.asyncio
async def test_retention_refuses_symlink_entries(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    target = tmp_path / "outside"
    target.write_text("outside")
    (root / "malicious").symlink_to(target)
    with pytest.raises(SecurityPolicyError, match="symlink"):
        await store.prune_orphans(
            known_artifact_ids=frozenset(),
            older_than=datetime.now(timezone.utc) + timedelta(days=1),
            dry_run=False,
        )
