"""Deterministic hierarchical context selection and compression."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import Field, model_validator

from durable_agent.domain.base import DomainModel, sha256_digest
from durable_agent.domain.context import ContextItem, ContextSnapshot, SummaryRecord
from durable_agent.domain.enums import SummaryLevel
from durable_agent.domain.errors import DomainValidationError
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.observability import METRICS, span

_PRESERVED_CATEGORIES = frozenset(
    {"constraint", "negative_requirement", "decision", "question", "task_state"}
)


class ContextBudget(DomainModel):
    """Fixed reservations and compressible capacity for a model request."""

    context_limit: int = Field(ge=2_048)
    reserved_output: int = Field(ge=128)
    system_instructions: int = Field(ge=128)
    user_request: int = Field(ge=128)
    compression_threshold: float = Field(default=0.8, gt=0.2, le=0.95)

    @model_validator(mode="after")
    def reservations_fit(self) -> ContextBudget:
        if self.available <= 0:
            raise ValueError("context reservations consume the entire window")
        return self

    @property
    def available(self) -> int:
        return (
            self.context_limit - self.reserved_output - self.system_instructions - self.user_request
        )

    @property
    def compression_at(self) -> int:
        return math.floor(self.available * self.compression_threshold)


class ContextManager:
    """Select mandatory material first and summarize only lower-priority history."""

    def __init__(
        self,
        *,
        budget: ContextBudget,
        identifiers: IdentifierGenerator,
        clock: Clock,
    ) -> None:
        self._budget = budget
        self._ids = identifiers
        self._clock = clock

    def create_item(
        self,
        *,
        category: str,
        content: str,
        priority: int = 100,
        source_refs: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
        mandatory: bool = False,
    ) -> ContextItem:
        """Create a token-estimated context item."""
        return ContextItem(
            item_id=self._ids.new("context-item"),
            category=category,
            content=content,
            estimated_tokens=self.estimate_tokens(content),
            priority=priority,
            source_refs=tuple(source_refs),
            evidence_ids=tuple(evidence_ids),
            mandatory=mandatory,
            created_at=self._clock.now(),
        )

    def build(
        self,
        *,
        run_id: str,
        items: Sequence[ContextItem],
        task_id: str | None = None,
        level: SummaryLevel = SummaryLevel.TASK,
        generation: int = 1,
    ) -> tuple[ContextSnapshot, SummaryRecord | None]:
        """Build a bounded snapshot and optional summary manifest."""
        unique = {item.item_id: item for item in items}
        if len(unique) != len(items):
            raise DomainValidationError("context item IDs must be unique")
        mandatory = sorted(
            (item for item in items if item.mandatory or item.category in _PRESERVED_CATEGORIES),
            key=lambda item: (item.priority, item.item_id),
        )
        optional = sorted(
            (item for item in items if item not in mandatory),
            key=lambda item: (item.priority, item.item_id),
        )
        mandatory_tokens = sum(item.estimated_tokens for item in mandatory)
        if mandatory_tokens > self._budget.available:
            raise DomainValidationError(
                "mandatory constraints and active state exceed the context budget"
            )
        total = mandatory_tokens + sum(item.estimated_tokens for item in optional)
        if total <= self._budget.compression_at:
            active_items = (*mandatory, *optional)
            snapshot = ContextSnapshot(
                context_id=self._ids.new("context"),
                run_id=run_id,
                task_id=task_id,
                budget_tokens=self._budget.available,
                used_tokens=total,
                item_ids=tuple(item.item_id for item in active_items),
                summary_ids=self._summary_references(active_items),
                created_at=self._clock.now(),
            )
            return snapshot, None

        selected = list(mandatory)
        used = mandatory_tokens
        # Reserve up to 20% for a navigation summary, without displacing constraints.
        summary_reserve = min(max(self._budget.available // 5, 64), 1_024)
        optional_limit = max(self._budget.available - summary_reserve, used)
        for item in optional:
            if used + item.estimated_tokens <= optional_limit:
                selected.append(item)
                used += item.estimated_tokens
        selected_ids = {item.item_id for item in selected}
        removed = [item for item in optional if item.item_id not in selected_ids]
        with span("context.compress", {"run.id": run_id, "context.removed": len(removed)}):
            summary = self._compress(
                run_id=run_id,
                removed=removed,
                level=level,
                generation=generation,
                maximum_tokens=self._budget.available - used,
            )
        METRICS.context_compressions.inc()
        used += summary.estimated_tokens if summary else 0
        retained_summary_ids = self._summary_references(selected)
        snapshot = ContextSnapshot(
            context_id=self._ids.new("context"),
            run_id=run_id,
            task_id=task_id,
            budget_tokens=self._budget.available,
            used_tokens=used,
            item_ids=tuple(item.item_id for item in selected),
            summary_ids=(
                (*retained_summary_ids, summary.summary_id) if summary else retained_summary_ids
            ),
            removed_item_ids=tuple(item.item_id for item in removed),
            created_at=self._clock.now(),
        )
        METRICS.token_estimates.inc(used)
        return snapshot, summary

    def invalidate_if_stale(
        self, summary: SummaryRecord, current_source_hashes: Mapping[str, str]
    ) -> SummaryRecord:
        """Invalidate when any primary source changed or disappeared."""
        stale = {
            source
            for source, digest in summary.source_hashes.items()
            if current_source_hashes.get(source) != digest
        }
        if not stale:
            return summary
        return summary.model_copy(
            update={
                "valid": False,
                "invalidated_reason": f"source content changed or disappeared: {sorted(stale)}",
            }
        )

    def _compress(
        self,
        *,
        run_id: str,
        removed: Sequence[ContextItem],
        level: SummaryLevel,
        generation: int,
        maximum_tokens: int,
    ) -> SummaryRecord | None:
        if not removed or maximum_tokens < 16:
            return None
        lines = ["Compressed navigation summary; verify conclusions against source/evidence IDs:"]
        for item in removed:
            compact = " ".join(item.content.split())
            lines.append(f"- [{item.item_id}] {compact}")
        content = "\n".join(lines)
        maximum_characters = maximum_tokens * 4
        if len(content) > maximum_characters:
            content = content[: max(maximum_characters - 20, 1)] + "… [truncated]"
        estimated = min(self.estimate_tokens(content), maximum_tokens)
        constraints = tuple(
            item.content
            for item in removed
            if item.category in {"constraint", "negative_requirement"}
        )
        # Preserved categories are normally raw; repeated summaries still record the
        # explicit sentinel to prevent silent constraint loss.
        if generation > 1 and not constraints:
            constraints = ("No removed constraint; raw constraints remain in active context.",)
        return SummaryRecord(
            summary_id=self._ids.new("summary"),
            run_id=run_id,
            level=level,
            content=content,
            estimated_tokens=estimated,
            source_item_ids=tuple(item.item_id for item in removed),
            source_hashes={item.item_id: sha256_digest(item.content) for item in removed},
            source_summary_ids=tuple(
                dict.fromkeys(
                    reference
                    for item in removed
                    if item.category == "summary"
                    for reference in item.source_refs
                )
            ),
            retained_constraints=constraints,
            retained_questions=tuple(
                item.content for item in removed if item.category == "question"
            ),
            retained_decisions=tuple(
                item.content for item in removed if item.category == "decision"
            ),
            evidence_ids=tuple(
                sorted({evidence for item in removed for evidence in item.evidence_ids})
            ),
            removed_item_ids=tuple(item.item_id for item in removed),
            generation=generation,
            created_at=self._clock.now(),
        )

    @staticmethod
    def _summary_references(items: Sequence[ContextItem]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                reference
                for item in items
                if item.category == "summary"
                for reference in item.source_refs
            )
        )

    @staticmethod
    def estimate_tokens(content: str) -> int:
        """Conservative deterministic approximation suitable for offline budgeting."""
        return max(math.ceil(len(content.encode("utf-8")) / 4), 1) if content else 0
