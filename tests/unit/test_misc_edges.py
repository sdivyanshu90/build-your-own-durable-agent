from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from durable_agent.domain.base import canonical_json
from durable_agent.domain.context import ContextSnapshot, SummaryRecord
from durable_agent.domain.enums import SummaryLevel
from durable_agent.domain.errors import NotFoundError
from durable_agent.tools.filesystem import RepositoryFileReaderTool
from durable_agent.tools.registry import ToolRegistry


def test_canonical_json_handles_utc_datetime_and_rejects_unknown_object() -> None:
    rendered = canonical_json({"at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    assert b"2026-01-01T00:00:00Z" in rendered
    with pytest.raises(TypeError, match="Unsupported canonical JSON"):
        canonical_json({"unknown": object()})


def test_context_schema_rejects_drift_and_budget_overflow() -> None:
    with pytest.raises(ValidationError, match="exactly cover"):
        SummaryRecord(
            summary_id="summary",
            run_id="run",
            level=SummaryLevel.TASK,
            content="summary",
            estimated_tokens=1,
            source_item_ids=("one",),
            source_hashes={},
        )
    with pytest.raises(ValidationError, match="explicitly retain"):
        SummaryRecord(
            summary_id="summary",
            run_id="run",
            level=SummaryLevel.RUN,
            content="summary",
            estimated_tokens=1,
            source_item_ids=("one",),
            source_hashes={"one": "a" * 64},
            generation=2,
        )
    with pytest.raises(ValidationError, match="exceeds its budget"):
        ContextSnapshot(
            context_id="context",
            run_id="run",
            budget_tokens=10,
            used_tokens=11,
            item_ids=(),
        )


def test_tool_registry_duplicate_unknown_and_sorted_definitions(tmp_path: Path) -> None:
    tool = RepositoryFileReaderTool(tmp_path)
    registry = ToolRegistry((tool,))
    assert registry.definitions()[0].name == "repository.read"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)
    with pytest.raises(NotFoundError, match="unknown tool"):
        registry.get("missing")
