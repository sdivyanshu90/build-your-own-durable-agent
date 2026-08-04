from __future__ import annotations

import pytest
from pydantic import ValidationError

from durable_agent.domain.enums import SideEffectClass
from durable_agent.domain.models import RepositoryChunk, ToolDefinition


def test_repository_chunk_line_range() -> None:
    with pytest.raises(ValidationError, match="end_line"):
        RepositoryChunk(
            chunk_id="chunk-1",
            file_id="file-1",
            snapshot_id="snap-1",
            relative_path="a.py",
            content="pass",
            content_hash="a" * 64,
            start_line=4,
            end_line=2,
        )


def test_non_idempotent_tool_cannot_be_retry_safe() -> None:
    with pytest.raises(ValidationError, match="retry safety"):
        ToolDefinition(
            name="external.publish",
            description="publish externally",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            timeout_seconds=10,
            required_permissions=frozenset({"network"}),
            side_effect_class=SideEffectClass.NON_IDEMPOTENT,
            retry_safe=True,
            produces_evidence=True,
        )


def test_nul_is_rejected_at_domain_boundary() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        ToolDefinition(
            name="repo.read",
            description="bad\x00description",
            input_schema={},
            output_schema={},
            timeout_seconds=1,
            required_permissions=frozenset(),
            side_effect_class=SideEffectClass.NONE,
            retry_safe=True,
            produces_evidence=True,
        )
