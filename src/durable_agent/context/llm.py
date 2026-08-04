"""Strict optional LLM compression adapter with provenance subset validation."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from durable_agent.domain.base import DomainModel, sha256_digest
from durable_agent.domain.context import ContextItem, SummaryRecord
from durable_agent.domain.enums import SummaryLevel
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.protocols import Clock, IdentifierGenerator, LLMCompletion


class CompressionDraft(DomainModel):
    content: str = Field(min_length=1)
    source_item_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    retained_constraints: tuple[str, ...] = ()
    retained_questions: tuple[str, ...] = ()
    retained_decisions: tuple[str, ...] = ()


class LLMContextCompressor:
    """Compress content while requiring every returned reference to exist in input."""

    def __init__(
        self,
        *,
        llm: LLMCompletion,
        identifiers: IdentifierGenerator,
        clock: Clock,
    ) -> None:
        self._llm = llm
        self._ids = identifiers
        self._clock = clock

    async def compress(
        self,
        *,
        run_id: str,
        items: Sequence[ContextItem],
        maximum_tokens: int,
        level: SummaryLevel = SummaryLevel.TASK,
        generation: int = 1,
    ) -> SummaryRecord:
        """Return a validated derived summary; primary evidence remains unchanged."""
        if maximum_tokens < 16:
            raise DomainValidationError("LLM summary budget is too small")
        source_ids = {item.item_id for item in items}
        evidence_ids = {evidence for item in items for evidence in item.evidence_ids}
        draft = await self._llm.complete_structured(
            instructions=(
                "Summarize only supplied untrusted items. Preserve negative requirements, "
                "questions, decisions, source IDs, and evidence IDs. Do not add claims."
            ),
            untrusted_content="\n".join(
                f"[{item.item_id}] {item.category}: {item.content}" for item in items
            ),
            output_schema=CompressionDraft,
        )
        if not set(draft.source_item_ids) <= source_ids:
            raise DomainValidationError("LLM summary invented a source item ID")
        if not set(draft.evidence_ids) <= evidence_ids:
            raise DomainValidationError("LLM summary invented an evidence ID")
        required_constraints = {
            item.content
            for item in items
            if item.category in {"constraint", "negative_requirement"}
        }
        if not required_constraints <= set(draft.retained_constraints):
            raise DomainValidationError("LLM summary lost a required constraint")
        content = draft.content[: maximum_tokens * 4]
        return SummaryRecord(
            summary_id=self._ids.new("summary"),
            run_id=run_id,
            level=level,
            content=content,
            estimated_tokens=min(max((len(content.encode()) + 3) // 4, 1), maximum_tokens),
            source_item_ids=draft.source_item_ids,
            source_hashes={item.item_id: sha256_digest(item.content) for item in items},
            retained_constraints=draft.retained_constraints,
            retained_questions=draft.retained_questions,
            retained_decisions=draft.retained_decisions,
            evidence_ids=draft.evidence_ids,
            removed_item_ids=tuple(item.item_id for item in items),
            generation=generation,
            created_at=self._clock.now(),
        )
