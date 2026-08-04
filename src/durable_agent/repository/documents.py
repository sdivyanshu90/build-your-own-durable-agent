"""Optional document adapters kept outside the core indexer's dependency set."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from pydantic import Field

from durable_agent.domain.base import DomainModel, sha256_digest
from durable_agent.domain.errors import DomainValidationError, ToolExecutionError
from durable_agent.security.paths import open_readonly_no_follow, resolve_within_root
from durable_agent.security.untrusted import classify_untrusted


class ExtractedDocument(DomainModel):
    """Normalized document text with page provenance and injection warnings."""

    relative_path: str
    media_type: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class PdfDocumentAdapter:
    """Extract bounded PDF page text using the optional ``pypdf`` extra."""

    def __init__(
        self,
        root: Path,
        *,
        maximum_bytes: int = 20_000_000,
        maximum_pages: int = 1_000,
        maximum_characters: int = 10_000_000,
    ) -> None:
        self._root = root.resolve(strict=True)
        self._maximum_bytes = maximum_bytes
        self._maximum_pages = maximum_pages
        self._maximum_characters = maximum_characters

    def extract(self, path: str | Path) -> ExtractedDocument:
        """Extract text without allowing the document to acquire instruction authority."""
        target = resolve_within_root(self._root, path)
        raw = open_readonly_no_follow(target)
        if len(raw) > self._maximum_bytes:
            raise DomainValidationError("PDF exceeds configured byte limit")
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ToolExecutionError(
                "PDF support is optional; install durable-coding-research-agent[pdf]"
            ) from exc
        try:
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise DomainValidationError("encrypted PDFs are not supported")
            if len(reader.pages) > self._maximum_pages:
                raise DomainValidationError("PDF exceeds configured page limit")
            pages: list[str] = []
            total = 0
            warnings: list[str] = []
            for number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                total += len(text)
                if total > self._maximum_characters:
                    raise DomainValidationError("PDF extracted text exceeds configured limit")
                pages.append(text)
                classification = classify_untrusted(f"{target.name}:page:{number}", text)
                if classification.injection_indicators:
                    warnings.append(
                        f"page {number} contains prompt-injection-like text; treated as data"
                    )
        except DomainValidationError:
            raise
        except Exception as exc:
            raise ToolExecutionError("PDF parsing failed safely") from exc
        return ExtractedDocument(
            relative_path=target.relative_to(self._root).as_posix(),
            media_type="application/pdf",
            content_hash=sha256_digest(raw),
            pages=tuple(pages),
            warnings=tuple(warnings),
        )
