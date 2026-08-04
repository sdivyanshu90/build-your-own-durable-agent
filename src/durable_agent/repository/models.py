"""Repository index aggregates and summaries."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from durable_agent.domain.base import DomainModel, utc_now
from durable_agent.domain.models import RepositoryChunk, RepositorySnapshot


class FileSummary(DomainModel):
    summary_id: str
    snapshot_id: str
    relative_path: str
    source_hash: str
    text: str
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    valid: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class RepositoryIndex(DomainModel):
    """Complete index output, including deleted tombstones and warnings."""

    snapshot: RepositorySnapshot
    chunks: tuple[RepositoryChunk, ...]
    summaries: tuple[FileSummary, ...]
    repository_map: str
    module_dependencies: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def chunks_for_file(self, relative_path: str) -> tuple[RepositoryChunk, ...]:
        return tuple(item for item in self.chunks if item.relative_path == relative_path)
