"""Bounded, incremental, provenance-preserving repository indexing."""

from __future__ import annotations

import ast
import mimetypes
import os
from collections.abc import Iterable
from pathlib import Path

import pathspec

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.enums import FileChangeKind
from durable_agent.domain.errors import DomainValidationError, SecurityPolicyError
from durable_agent.domain.models import RepositoryChunk, RepositoryFile, RepositorySnapshot
from durable_agent.domain.protocols import IdentifierGenerator
from durable_agent.repository.models import FileSummary, RepositoryIndex
from durable_agent.security.paths import open_readonly_no_follow, resolve_within_root
from durable_agent.security.untrusted import classify_untrusted

_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".sh": "shell",
    ".sql": "sql",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".ini": "ini",
    ".cfg": "ini",
}
_CONFIG_NAMES = {
    "Dockerfile",
    "Makefile",
    "Procfile",
    ".gitignore",
    ".dockerignore",
    ".env.example",
}


class LocalRepositoryIndexer:
    """Index local text repositories without crossing their configured root."""

    def __init__(
        self,
        *,
        identifiers: IdentifierGenerator,
        maximum_file_bytes: int = 2_000_000,
        maximum_repository_bytes: int = 100_000_000,
        exclusions: Iterable[str] = (),
        chunk_lines: int = 80,
    ) -> None:
        if maximum_file_bytes <= 0 or maximum_repository_bytes <= 0 or chunk_lines <= 0:
            raise ValueError("indexing limits must be positive")
        self._ids = identifiers
        self._maximum_file_bytes = maximum_file_bytes
        self._maximum_repository_bytes = maximum_repository_bytes
        self._exclusions = tuple(exclusions)
        self._chunk_lines = chunk_lines

    async def index(
        self,
        root: Path,
        *,
        previous: RepositoryIndex | None = None,
        snapshot_id: str | None = None,
    ) -> RepositoryIndex:
        """Build a deterministic snapshot, comparing it with a prior index if supplied."""
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise DomainValidationError(f"repository root is not a directory: {root}")
        ignore_patterns = self._load_ignore_patterns(root)
        previous_by_path = (
            {item.relative_path: item for item in previous.snapshot.files if not item.is_deleted}
            if previous
            else {}
        )
        snapshot_id = snapshot_id or self._ids.new("snap")
        if not snapshot_id.strip() or len(snapshot_id) > 256:
            raise DomainValidationError("snapshot ID must contain 1 to 256 characters")
        files: list[RepositoryFile] = []
        chunks: list[RepositoryChunk] = []
        summaries: list[FileSummary] = []
        dependencies: dict[str, tuple[str, ...]] = {}
        warnings: list[str] = []
        total_bytes = 0
        seen: set[str] = set()

        for relative, absolute in self._walk(root, ignore_patterns, warnings):
            size = absolute.stat(follow_symlinks=False).st_size
            if size > self._maximum_file_bytes:
                warnings.append(f"skipped oversized file: {relative} ({size} bytes)")
                continue
            total_bytes += size
            if total_bytes > self._maximum_repository_bytes:
                raise DomainValidationError(
                    f"repository exceeds {self._maximum_repository_bytes} byte indexing limit"
                )
            raw = open_readonly_no_follow(absolute)
            if len(raw) != size:
                raise SecurityPolicyError(f"file changed while indexing: {relative}")
            text = self._decode_text(raw)
            if text is None:
                warnings.append(f"skipped unsupported binary file: {relative}")
                continue
            content_hash = sha256_digest(raw)
            prior = previous_by_path.get(relative)
            change = (
                FileChangeKind.NEW
                if prior is None
                else FileChangeKind.UNCHANGED
                if prior.content_hash == content_hash
                else FileChangeKind.MODIFIED
            )
            file_id = self._stable_file_id(relative)
            language, media_type = self._detect_type(absolute)
            file = RepositoryFile(
                file_id=file_id,
                snapshot_id=snapshot_id,
                relative_path=relative,
                content_hash=content_hash,
                size_bytes=size,
                media_type=media_type,
                language=language,
                change_kind=change,
            )
            files.append(file)
            seen.add(relative)
            file_chunks, symbols, imports = self._chunks_for(
                file=file, text=text, snapshot_id=snapshot_id
            )
            chunks.extend(file_chunks)
            dependencies[relative] = imports
            summaries.append(
                FileSummary(
                    summary_id=self._ids.new("reposum"),
                    snapshot_id=snapshot_id,
                    relative_path=relative,
                    source_hash=content_hash,
                    text=self._summarize(relative, language, text, symbols, imports),
                    symbols=symbols,
                    imports=imports,
                )
            )
            classified = classify_untrusted(relative, text)
            if classified.injection_indicators:
                warnings.append(
                    f"prompt-injection indicators in untrusted file: {relative} "
                    f"({len(classified.injection_indicators)})"
                )

        for relative, prior in sorted(previous_by_path.items()):
            if relative in seen:
                continue
            files.append(
                prior.model_copy(
                    update={
                        "snapshot_id": snapshot_id,
                        "change_kind": FileChangeKind.DELETED,
                        "is_deleted": True,
                    }
                )
            )

        files.sort(key=lambda item: item.relative_path)
        manifest = [
            {"path": item.relative_path, "hash": item.content_hash, "deleted": item.is_deleted}
            for item in files
        ]
        snapshot = RepositorySnapshot(
            snapshot_id=snapshot_id,
            root=str(root),
            manifest_hash=sha256_digest(canonical_json(manifest)),
            file_count=sum(not item.is_deleted for item in files),
            total_bytes=total_bytes,
            files=tuple(files),
        )
        return RepositoryIndex(
            snapshot=snapshot,
            chunks=tuple(chunks),
            summaries=tuple(summaries),
            repository_map=self._repository_map(files, summaries),
            module_dependencies=dependencies,
            warnings=tuple(warnings),
        )

    def _walk(
        self, root: Path, patterns: list[str], warnings: list[str]
    ) -> Iterable[tuple[str, Path]]:
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            if current_path != root:
                nested = current_path / ".gitignore"
                if nested.is_file() and not nested.is_symlink():
                    raw = open_readonly_no_follow(nested)
                    if len(raw) <= self._maximum_file_bytes:
                        prefix = current_path.relative_to(root).as_posix()
                        patterns.extend(
                            self._scoped_ignore_pattern(prefix, line)
                            for line in raw.decode("utf-8", errors="replace").splitlines()
                        )
            ignore = pathspec.GitIgnoreSpec.from_lines(patterns)
            safe_directories = []
            for name in sorted(directories):
                candidate = current_path / name
                relative = candidate.relative_to(root).as_posix() + "/"
                if candidate.is_symlink():
                    warnings.append(f"skipped directory symlink: {relative}")
                elif not ignore.match_file(relative):
                    safe_directories.append(name)
            directories[:] = safe_directories
            for name in sorted(filenames):
                candidate = current_path / name
                relative = candidate.relative_to(root).as_posix()
                if ignore.match_file(relative):
                    continue
                if candidate.is_symlink():
                    warnings.append(f"skipped file symlink: {relative}")
                    continue
                try:
                    resolved = resolve_within_root(root, candidate)
                except SecurityPolicyError:
                    warnings.append(f"skipped unsafe path: {relative}")
                    continue
                if not resolved.is_file():
                    warnings.append(f"skipped non-regular file: {relative}")
                    continue
                yield relative, resolved

    def _load_ignore_patterns(self, root: Path) -> list[str]:
        patterns = list(self._exclusions)
        ignore_path = root / ".gitignore"
        if ignore_path.is_file() and not ignore_path.is_symlink():
            raw = open_readonly_no_follow(ignore_path)
            if len(raw) <= self._maximum_file_bytes:
                patterns.extend(raw.decode("utf-8", errors="replace").splitlines())
        return patterns

    @staticmethod
    def _scoped_ignore_pattern(prefix: str, pattern: str) -> str:
        """Scope a nested .gitignore pattern to the directory that declares it."""
        if not pattern or pattern.startswith("#"):
            return pattern
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        body = body.removeprefix("/")
        scoped = f"{prefix}/{body}"
        return f"!{scoped}" if negated else scoped

    @staticmethod
    def _decode_text(raw: bytes) -> str | None:
        if b"\x00" in raw[:8_192]:
            return None
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            printable = sum(byte in b"\t\n\r" or 32 <= byte < 127 for byte in raw[:8_192])
            if raw and printable / min(len(raw), 8_192) < 0.85:
                return None
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _detect_type(path: Path) -> tuple[str | None, str]:
        suffix = path.suffix.lower()
        language = _LANGUAGES.get(suffix)
        if language is None and path.name in _CONFIG_NAMES:
            language = "configuration"
        media_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        if media_type == "application/octet-stream" and language:
            media_type = "text/plain"
        return language, media_type

    def _chunks_for(
        self, *, file: RepositoryFile, text: str, snapshot_id: str
    ) -> tuple[list[RepositoryChunk], tuple[str, ...], tuple[str, ...]]:
        lines = text.splitlines()
        if not lines:
            lines = [""]
        symbols: list[tuple[str, int, int]] = []
        imports: set[str] = set()
        if file.language == "python":
            try:
                tree = ast.parse(text, filename=file.relative_path)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                        symbols.append(
                            (
                                node.name,
                                node.lineno,
                                getattr(node, "end_lineno", node.lineno),
                            )
                        )
                    elif isinstance(node, ast.Import):
                        imports.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module)
        result: list[RepositoryChunk] = []
        ranges = list(symbols)
        if symbols:
            covered = {
                line for _, start, end in symbols for line in range(start, min(end, len(lines)) + 1)
            }
            uncovered = [line for line in range(1, len(lines) + 1) if line not in covered]
            start_index = 0
            while start_index < len(uncovered):
                start = uncovered[start_index]
                end = start
                start_index += 1
                while (
                    start_index < len(uncovered)
                    and uncovered[start_index] == end + 1
                    and end - start + 1 < self._chunk_lines
                ):
                    end = uncovered[start_index]
                    start_index += 1
                ranges.append(("", start, end))
        else:
            ranges = [
                ("", start, min(start + self._chunk_lines - 1, len(lines)))
                for start in range(1, len(lines) + 1, self._chunk_lines)
            ]
        ranges.sort(key=lambda item: (item[1], item[2], item[0]))
        for symbol, start, end in ranges:
            content = "\n".join(lines[start - 1 : end])
            result.append(
                RepositoryChunk(
                    chunk_id=self._stable_chunk_id(snapshot_id, file.relative_path, start, end),
                    file_id=file.file_id,
                    snapshot_id=snapshot_id,
                    relative_path=file.relative_path,
                    content=content,
                    content_hash=sha256_digest(content),
                    start_line=start,
                    end_line=end,
                    symbol_name=symbol or None,
                    language=file.language,
                    imports=tuple(sorted(imports)),
                )
            )
        return result, tuple(item[0] for item in symbols), tuple(sorted(imports))

    @staticmethod
    def _summarize(
        relative: str,
        language: str | None,
        text: str,
        symbols: tuple[str, ...],
        imports: tuple[str, ...],
    ) -> str:
        first_content = next(
            (line.strip("# */\t") for line in text.splitlines() if line.strip()), ""
        )
        parts = [f"{relative} is {language or 'text'} ({len(text.splitlines())} lines)."]
        if first_content:
            parts.append(f"Opening content: {first_content[:160]}.")
        if symbols:
            parts.append(f"Defines: {', '.join(symbols[:20])}.")
        if imports:
            parts.append(f"Imports: {', '.join(imports[:20])}.")
        return " ".join(parts)

    @staticmethod
    def _repository_map(files: list[RepositoryFile], summaries: list[FileSummary]) -> str:
        summary_by_path = {summary.relative_path: summary for summary in summaries}
        output = ["# Repository map"]
        for file in files:
            marker = "deleted" if file.is_deleted else file.language or file.media_type
            output.append(f"- `{file.relative_path}` ({marker}, {file.size_bytes} bytes)")
            summary = summary_by_path.get(file.relative_path)
            if summary and summary.symbols:
                output.append(f"  - symbols: {', '.join(summary.symbols)}")
        return "\n".join(output) + "\n"

    @staticmethod
    def _stable_file_id(relative: str) -> str:
        return f"file-{sha256_digest(relative)[:24]}"

    @staticmethod
    def _stable_chunk_id(snapshot_id: str, relative: str, start: int, end: int) -> str:
        return f"chunk-{sha256_digest(f'{snapshot_id}:{relative}:{start}:{end}')[:24]}"
