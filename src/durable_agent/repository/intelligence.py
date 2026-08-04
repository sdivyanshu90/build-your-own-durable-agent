"""Deterministic repository questions with primary line provenance."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from durable_agent.domain.base import DomainModel
from durable_agent.domain.models import RetrievalItem
from durable_agent.repository.models import RepositoryIndex
from durable_agent.retrieval import RetrievalEngine


class RepositoryClaim(DomainModel):
    """One bounded repository observation and the chunks supporting it."""

    statement: str
    evidence: tuple[RetrievalItem, ...] = Field(min_length=1)
    inference: bool = False


class RepositoryAnswer(DomainModel):
    """A deterministic answer whose every observation carries provenance."""

    question: str
    answer: str
    claims: tuple[RepositoryClaim, ...]
    snapshot_id: str
    limitations: tuple[str, ...] = (
        "Retrieval identifies relevant source locations; semantic conclusions require review.",
    )


class RepositoryIntelligence:
    """Answer common purpose, dependency, test, impact, and configuration questions."""

    def __init__(self, index: RepositoryIndex, *, retrieval: RetrievalEngine | None = None) -> None:
        self._index = index
        self._retrieval = retrieval or RetrievalEngine(index.chunks)

    async def answer(self, question: str, *, limit: int = 8) -> RepositoryAnswer:
        """Return relevant source observations without inventing unsupported conclusions."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be blank")
        if limit < 1:
            raise ValueError("limit must be positive")
        candidates = self._structural_candidates(question)
        candidates.extend(await self._retrieval.hybrid(question, limit=limit))
        selected = self._unique(candidates)[:limit]
        claims = tuple(
            RepositoryClaim(
                statement=self._statement(item, question),
                evidence=(item,),
                inference=True,
            )
            for item in selected
        )
        answer = (
            "No indexed source location matched the question."
            if not claims
            else "Relevant primary locations: "
            + ", ".join(item.evidence[0].source_location for item in claims)
            + "."
        )
        return RepositoryAnswer(
            question=question,
            answer=answer,
            claims=claims,
            snapshot_id=self._index.snapshot.snapshot_id,
        )

    def _structural_candidates(self, question: str) -> list[RetrievalItem]:
        lowered = question.lower()
        paths: set[str] = set()
        if any(term in lowered for term in ("purpose", "what does", "overview")):
            paths.update(
                summary.relative_path
                for summary in self._index.summaries
                if summary.relative_path.lower() in {"readme.md", "pyproject.toml"}
            )
        if any(term in lowered for term in ("test", "cover", "behavior")):
            paths.update(
                chunk.relative_path
                for chunk in self._index.chunks
                if "test" in chunk.relative_path.lower()
            )
        if any(term in lowered for term in ("config", "operational", "assumption")):
            paths.update(
                summary.relative_path
                for summary in self._index.summaries
                if summary.relative_path.lower().endswith(
                    (".yaml", ".yml", ".toml", ".ini", ".cfg", ".env.example")
                )
                or summary.relative_path.rsplit("/", maxsplit=1)[-1] in {"Dockerfile", "Makefile"}
            )
        for path, imports in self._index.module_dependencies.items():
            if any(imported.lower() in lowered for imported in imports):
                paths.add(path)
        return [
            self._retrieval_item(chunk)
            for chunk in self._index.chunks
            if chunk.relative_path in paths
        ]

    @staticmethod
    def _unique(items: Sequence[RetrievalItem]) -> list[RetrievalItem]:
        by_id: dict[str, RetrievalItem] = {}
        for item in items:
            by_id.setdefault(item.item_id, item)
        return list(by_id.values())

    @staticmethod
    def _statement(item: RetrievalItem, question: str) -> str:
        del question
        symbol = item.metadata.get("symbol")
        detail = f" symbol {symbol}" if isinstance(symbol, str) and symbol else ""
        return f"{item.source_location} contains{detail} relevant to the repository question."

    @staticmethod
    def _retrieval_item(chunk: object) -> RetrievalItem:
        from durable_agent.domain.models import RepositoryChunk

        if not isinstance(chunk, RepositoryChunk):
            raise TypeError("repository index contained an invalid chunk")
        return RetrievalItem(
            item_id=chunk.chunk_id,
            source_type="repository_chunk",
            source=chunk.relative_path,
            source_location=f"{chunk.relative_path}:{chunk.start_line}-{chunk.end_line}",
            content=chunk.content,
            content_hash=chunk.content_hash,
            score=0.0,
            snapshot_id=chunk.snapshot_id,
            metadata={"symbol": chunk.symbol_name, "language": chunk.language},
        )
