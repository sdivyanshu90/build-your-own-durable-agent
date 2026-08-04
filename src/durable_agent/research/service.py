"""Offline-capable research ingestion, deduplication, facts, and conflicts."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.models import RetrievalItem
from durable_agent.domain.protocols import Clock, IdentifierGenerator, SearchProvider
from durable_agent.research.models import ResearchConflict, ResearchFact, ResearchSource
from durable_agent.security.untrusted import classify_untrusted

_CLAIM_LINE = re.compile(r"(?im)^\s*(?:claim|fact)\s*:\s*([^=:\n]+)\s*[=:]\s*(.+?)\s*$")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


class ResearchService:
    """Treat provider content as data, preserving source metadata and disagreement."""

    def __init__(
        self,
        *,
        provider: SearchProvider,
        identifiers: IdentifierGenerator,
        clock: Clock,
    ) -> None:
        self._provider = provider
        self._ids = identifiers
        self._clock = clock
        self._sources_by_hash: dict[str, ResearchSource] = {}

    async def research(
        self, query: str, *, limit: int = 10
    ) -> tuple[tuple[ResearchSource, ...], tuple[ResearchFact, ...], tuple[ResearchConflict, ...]]:
        """Search, normalize, deduplicate, and identify explicit fixture/provider claims."""
        items = await self._provider.search(query, limit=limit)
        sources = tuple(self.ingest(item) for item in items)
        unique = tuple({source.source_id: source for source in sources}.values())
        facts = self.extract_facts(unique)
        conflicts = self.detect_conflicts(facts)
        conflicting_keys = {conflict.key for conflict in conflicts}
        facts = tuple(
            fact.model_copy(
                update={"verified": len(fact.source_ids) > 1 and fact.key not in conflicting_keys}
            )
            for fact in facts
        )
        return unique, facts, conflicts

    def ingest(self, item: RetrievalItem) -> ResearchSource:
        """Normalize one provider result and deduplicate by normalized content hash."""
        media_type = str(item.metadata.get("media_type", "text/plain"))
        normalized = self.normalize(item.content, media_type=media_type)
        digest = sha256_digest(normalized)
        existing = self._sources_by_hash.get(digest)
        if existing is not None:
            return existing
        classified = classify_untrusted(item.source, normalized)
        quality = self._quality(item)
        published = item.metadata.get("published_at")
        source = ResearchSource(
            source_id=self._ids.new("source"),
            url=self._canonical_url(item.source)
            if item.source.startswith(("http://", "https://"))
            else None,
            title=str(item.metadata.get("title", item.source)),
            normalized_content=normalized,
            content_hash=digest,
            author=self._optional_text(item.metadata.get("author")),
            publisher=self._optional_text(item.metadata.get("publisher")),
            published_at=published if isinstance(published, datetime) else None,
            retrieved_at=self._clock.now(),
            media_type=media_type,
            quality_score=quality[0],
            quality_notes=quality[1],
            injection_indicators=classified.injection_indicators,
        )
        self._sources_by_hash[digest] = source
        return source

    def extract_facts(self, sources: Sequence[ResearchSource]) -> tuple[ResearchFact, ...]:
        """Extract explicit `FACT: key = value` records without inferring truth."""
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for source in sources:
            for match in _CLAIM_LINE.finditer(source.normalized_content):
                key = " ".join(match.group(1).lower().split())
                value = " ".join(match.group(2).split())
                grouped[(key, value)].append(source.source_id)
        return tuple(
            ResearchFact(
                fact_id=self._ids.new("fact"),
                key=key,
                value=value,
                source_ids=tuple(sorted(set(source_ids))),
            )
            for (key, value), source_ids in sorted(grouped.items())
        )

    def detect_conflicts(self, facts: Sequence[ResearchFact]) -> tuple[ResearchConflict, ...]:
        """Flag keys for which sources assert multiple distinct values."""
        values: dict[str, dict[str, tuple[str, ...]]] = defaultdict(dict)
        for fact in facts:
            values[fact.key][fact.value] = fact.source_ids
        return tuple(
            ResearchConflict(
                conflict_id=self._ids.new("conflict"),
                key=key,
                values=by_value,
                explanation=f"Sources assert {len(by_value)} incompatible values for {key!r}.",
            )
            for key, by_value in sorted(values.items())
            if len(by_value) > 1
        )

    @staticmethod
    def citation(source: ResearchSource) -> str:
        """Render a stable source citation suitable for research notes."""
        location = str(source.url) if source.url else source.title
        return (
            f"[{source.source_id}] {source.title}. {location}. "
            f"Retrieved {source.retrieved_at.date()}."
        )

    @staticmethod
    def normalize(content: str, *, media_type: str) -> str:
        """Normalize HTML or text without executing active content."""
        if "html" in media_type.lower() or re.search(r"<html[\s>]", content, re.I):
            parser = _TextExtractor()
            parser.feed(content)
            content = "".join(parser.parts)
        content = html.unescape(content).replace("\r\n", "\n").replace("\r", "\n")
        lines = [" ".join(line.split()) for line in content.splitlines()]
        output: list[str] = []
        for line in lines:
            if line or (output and output[-1]):
                output.append(line)
        return "\n".join(output).strip()

    @staticmethod
    def _canonical_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))

    @staticmethod
    def _quality(item: RetrievalItem) -> tuple[float, tuple[str, ...]]:
        declared = item.metadata.get("quality_score")
        if isinstance(declared, float | int) and not isinstance(declared, bool):
            score = max(0.0, min(float(declared), 1.0))
            return score, ("provider-declared quality",)
        if item.source.startswith("https://"):
            return 0.6, ("HTTPS source; authority not independently verified",)
        return 0.4, ("local or non-HTTPS source",)

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return str(value) if value is not None else None
