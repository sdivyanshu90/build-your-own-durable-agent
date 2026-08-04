"""Validate the documentation corpus, local links, and depth/visualization contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
MERMAID_START = re.compile(
    r"^(?:flowchart|graph|sequenceDiagram|stateDiagram(?:-v2)?|erDiagram|"
    r"classDiagram|gantt|mindmap|timeline|journey|quadrantChart|"
    r"C4(?:Context|Container|Component|Dynamic|Deployment))\b"
)
REQUIRED = frozenset(
    {
        "README.md",
        "docs/index.md",
        "docs/api.md",
        "docs/architecture.md",
        "docs/checkpointing.md",
        "docs/cli.md",
        "docs/concepts.md",
        "docs/configuration.md",
        "docs/context-compression.md",
        "docs/data-model.md",
        "docs/evidence-and-reporting.md",
        "docs/glossary.md",
        "docs/operations.md",
        "docs/pause-resume-recovery.md",
        "docs/planning.md",
        "docs/repository-understanding.md",
        "docs/requirements.md",
        "docs/research-log.md",
        "docs/security.md",
        "docs/state-machine.md",
        "docs/testing.md",
        "docs/tools.md",
        "docs/troubleshooting.md",
        "docs/verification-results.md",
        "docs/adr/README.md",
        "docs/adr/0001-language-framework.md",
        "docs/adr/0002-persistence-event-audit.md",
        "docs/adr/0003-checkpoint-recovery.md",
        "docs/adr/0004-task-graph-concurrency.md",
        "docs/adr/0005-context-retrieval.md",
        "docs/adr/0006-tool-security.md",
        "docs/adr/0007-provider-abstraction.md",
        "docs/adr/0008-evidence-model.md",
        "docs/adr/0009-databases.md",
    }
)

FOCUSED_MINIMUM_WORDS = {
    "docs/index.md": 600,
    "docs/adr/README.md": 400,
}
DEFAULT_DOCUMENT_MINIMUM_WORDS = 800


def _word_count(content: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", content))


def _mermaid_blocks(content: str) -> list[str]:
    return re.findall(r"```mermaid\s*\n(.*?)```", content, flags=re.DOTALL)


def main() -> int:
    missing_required = sorted(path for path in REQUIRED if not (ROOT / path).is_file())
    markdown_files = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
    broken: list[str] = []
    quality_errors: list[str] = []
    checked = 0
    for source in markdown_files:
        content = source.read_text(encoding="utf-8")
        relative_source = source.relative_to(ROOT).as_posix()
        if relative_source.startswith("docs/"):
            minimum_words = FOCUSED_MINIMUM_WORDS.get(
                relative_source, DEFAULT_DOCUMENT_MINIMUM_WORDS
            )
            words = _word_count(content)
            if words < minimum_words:
                quality_errors.append(
                    f"{relative_source}: {words} words; minimum is {minimum_words}"
                )
            mermaid_blocks = _mermaid_blocks(content)
            if not mermaid_blocks:
                quality_errors.append(f"{relative_source}: no Mermaid visualization")
            elif any(not block.strip() for block in mermaid_blocks):
                quality_errors.append(f"{relative_source}: empty Mermaid visualization")
            for index, block in enumerate(mermaid_blocks, start=1):
                first_line = block.strip().splitlines()[0]
                if not MERMAID_START.match(first_line):
                    quality_errors.append(
                        f"{relative_source}: Mermaid block {index} has unknown "
                        f"diagram declaration {first_line!r}"
                    )
            fences = re.findall(r"^```", content, flags=re.MULTILINE)
            if len(fences) % 2 != 0:
                quality_errors.append(f"{relative_source}: unbalanced fenced code block")
            if "\n## " not in content:
                quality_errors.append(f"{relative_source}: no second-level sections")
        for raw_target in LINK.findall(content):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative_target = unquote(target.split("#", maxsplit=1)[0])
            resolved = (source.parent / relative_target).resolve()
            checked += 1
            if ROOT not in resolved.parents and resolved != ROOT:
                broken.append(f"{source.relative_to(ROOT)} -> {target} (escapes repository)")
            elif not resolved.exists():
                broken.append(f"{source.relative_to(ROOT)} -> {target}")
    if missing_required or broken or quality_errors:
        for path in missing_required:
            print(f"missing required document: {path}", file=sys.stderr)
        for link in broken:
            print(f"broken local link: {link}", file=sys.stderr)
        for error in quality_errors:
            print(f"documentation quality failure: {error}", file=sys.stderr)
        return 1
    docs = markdown_files[1:]
    diagrams = sum(len(_mermaid_blocks(path.read_text(encoding="utf-8"))) for path in docs)
    words = sum(_word_count(path.read_text(encoding="utf-8")) for path in docs)
    print(
        "documentation validation passed: "
        f"{len(markdown_files)} files, {checked} local links, "
        f"{words} documentation words, {diagrams} Mermaid visualizations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
