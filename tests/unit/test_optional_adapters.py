from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.context import CompressionDraft, LLMContextCompressor
from durable_agent.context.manager import ContextBudget, ContextManager
from durable_agent.domain.errors import DomainValidationError
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers, FakeLLM
from durable_agent.repository import PdfDocumentAdapter


@pytest.mark.asyncio
async def test_llm_compressor_rejects_invented_evidence() -> None:
    identifiers = DeterministicIdentifiers()
    clock = DeterministicClock()
    manager = ContextManager(
        budget=ContextBudget(
            context_limit=2_048,
            reserved_output=128,
            system_instructions=128,
            user_request=128,
        ),
        identifiers=identifiers,
        clock=clock,
    )
    item = manager.create_item(
        category="negative_requirement",
        content="Never access the network",
        evidence_ids=("EVID-real",),
    )
    compressor = LLMContextCompressor(
        llm=FakeLLM(
            [
                CompressionDraft(
                    content="No network access.",
                    source_item_ids=(item.item_id,),
                    evidence_ids=("EVID-forged",),
                    retained_constraints=(item.content,),
                )
            ]
        ),
        identifiers=identifiers,
        clock=clock,
    )
    with pytest.raises(DomainValidationError, match="invented an evidence"):
        await compressor.compress(run_id="run", items=(item,), maximum_tokens=100)


@pytest.mark.asyncio
async def test_llm_compressor_accepts_provenance_and_preserves_constraint() -> None:
    identifiers = DeterministicIdentifiers()
    clock = DeterministicClock()
    manager = ContextManager(
        budget=ContextBudget(
            context_limit=2_048,
            reserved_output=128,
            system_instructions=128,
            user_request=128,
        ),
        identifiers=identifiers,
        clock=clock,
    )
    item = manager.create_item(
        category="constraint", content="Preserve compatibility", evidence_ids=("EVID-1",)
    )
    draft = CompressionDraft(
        content="Compatibility remains required.",
        source_item_ids=(item.item_id,),
        evidence_ids=("EVID-1",),
        retained_constraints=(item.content,),
    )
    compressor = LLMContextCompressor(llm=FakeLLM([draft]), identifiers=identifiers, clock=clock)
    summary = await compressor.compress(run_id="run", items=(item,), maximum_tokens=100)
    assert summary.evidence_ids == ("EVID-1",)
    assert summary.retained_constraints == ("Preserve compatibility",)
    with pytest.raises(DomainValidationError, match="too small"):
        await compressor.compress(run_id="run", items=(item,), maximum_tokens=1)


@pytest.mark.asyncio
async def test_llm_compressor_rejects_lost_constraint() -> None:
    identifiers = DeterministicIdentifiers()
    clock = DeterministicClock()
    manager = ContextManager(
        budget=ContextBudget(
            context_limit=2_048,
            reserved_output=128,
            system_instructions=128,
            user_request=128,
        ),
        identifiers=identifiers,
        clock=clock,
    )
    item = manager.create_item(category="constraint", content="Do not break callers")
    compressor = LLMContextCompressor(
        llm=FakeLLM(
            [
                CompressionDraft(
                    content="A summary", source_item_ids=(item.item_id,), evidence_ids=()
                )
            ]
        ),
        identifiers=identifiers,
        clock=clock,
    )
    with pytest.raises(DomainValidationError, match="lost a required constraint"):
        await compressor.compress(run_id="run", items=(item,), maximum_tokens=100)


def test_optional_pdf_adapter_extracts_bounded_page(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    root = tmp_path / "documents"
    root.mkdir()
    target = root / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with target.open("wb") as handle:
        writer.write(handle)
    document = PdfDocumentAdapter(root).extract("blank.pdf")
    assert document.relative_path == "blank.pdf"
    assert document.media_type == "application/pdf"
    assert document.pages == ("",)


def test_pdf_adapter_enforces_byte_limit_before_parsing(tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "large.pdf").write_bytes(b"x" * 20)
    with pytest.raises(DomainValidationError, match="byte limit"):
        PdfDocumentAdapter(root, maximum_bytes=10).extract("large.pdf")
