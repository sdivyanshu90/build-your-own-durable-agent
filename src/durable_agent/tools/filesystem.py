"""Constrained repository read/write and content-addressed artifact tools."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.enums import SideEffectClass
from durable_agent.domain.errors import ToolExecutionError
from durable_agent.domain.models import ToolCall, ToolDefinition, ToolResult
from durable_agent.security.paths import open_readonly_no_follow, resolve_within_root


class RepositoryFileReaderTool:
    def __init__(self, root: Path, *, maximum_bytes: int = 2_000_000) -> None:
        self._root = root
        self._maximum_bytes = maximum_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repository.read",
            description="Read a UTF-8 repository file inside the approved root",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["path", "content", "content_hash"]},
            timeout_seconds=10,
            required_permissions=frozenset({"repository.read"}),
            side_effect_class=SideEffectClass.NONE,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = resolve_within_root(self._root, str(arguments["path"]))
        raw = open_readonly_no_follow(path)
        if len(raw) > self._maximum_bytes:
            raise ToolExecutionError("file exceeds reader size limit")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("file is not UTF-8 text") from exc
        return {
            "path": path.relative_to(self._root.resolve()).as_posix(),
            "content": content,
            "content_hash": sha256_digest(raw),
        }

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None


class ControlledFileWriterTool:
    """Atomic compare-and-swap file writer."""

    def __init__(self, root: Path, *, maximum_bytes: int = 2_000_000) -> None:
        self._root = root
        self._maximum_bytes = maximum_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repository.write",
            description="Atomically write a repository text file after expected-hash validation",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "expected_hash": {"type": ["string", "null"]},
                },
                "required": ["path", "content", "expected_hash"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["path", "content_hash", "bytes"]},
            timeout_seconds=10,
            required_permissions=frozenset({"repository.write"}),
            side_effect_class=SideEffectClass.IDEMPOTENT,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        content = str(arguments["content"]).encode("utf-8")
        if len(content) > self._maximum_bytes:
            raise ToolExecutionError("write exceeds file size limit")
        path = resolve_within_root(self._root, str(arguments["path"]), must_exist=False)
        expected = arguments["expected_hash"]
        if path.exists():
            current = open_readonly_no_follow(path)
            current_hash = sha256_digest(current)
            if expected is None or expected != current_hash:
                if current == content:
                    return self._result(path, content)
                raise ToolExecutionError("file changed since expected hash was captured")
        elif expected is not None:
            raise ToolExecutionError("expected hash supplied for a missing file")
        descriptor, temporary = tempfile.mkstemp(prefix=".durable-agent-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path = Path(temporary)
            temporary_path.chmod(0o600)
            temporary_path.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()
            raise
        return self._result(path, content)

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        path = resolve_within_root(self._root, str(call.arguments["path"]), must_exist=False)
        if not path.exists():
            return None
        expected_content = str(call.arguments["content"]).encode()
        if open_readonly_no_follow(path) != expected_content:
            return None
        output = self._result(path, expected_content)
        return ToolResult(
            tool_result_id=f"reconciled-{call.tool_call_id}",
            tool_call_id=call.tool_call_id,
            success=True,
            output=output,
            output_hash=sha256_digest(str(output)),
            duration_seconds=0,
        )

    def _result(self, path: Path, content: bytes) -> dict[str, Any]:
        return {
            "path": path.relative_to(self._root.resolve()).as_posix(),
            "content_hash": sha256_digest(content),
            "bytes": len(content),
        }


class PatchApplicationTool:
    """Apply one exact text replacement through the atomic writer contract."""

    def __init__(self, root: Path, *, maximum_bytes: int = 2_000_000) -> None:
        self._root = root
        self._writer = ControlledFileWriterTool(root, maximum_bytes=maximum_bytes)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repository.patch",
            description="Atomically replace one exact fragment in an existing repository file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "old": {"type": "string", "minLength": 1},
                    "new": {"type": "string"},
                    "expected_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "expected_result_hash": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
                "required": ["path", "old", "new", "expected_hash", "expected_result_hash"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["path", "content_hash", "bytes", "replacements"],
            },
            timeout_seconds=10,
            required_permissions=frozenset({"repository.patch"}),
            side_effect_class=SideEffectClass.IDEMPOTENT,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = resolve_within_root(self._root, str(arguments["path"]))
        raw = open_readonly_no_follow(path)
        current_hash = sha256_digest(raw)
        if current_hash != arguments["expected_hash"]:
            if current_hash == arguments["expected_result_hash"]:
                return {**self._result(path, raw), "replacements": 0}
            raise ToolExecutionError("file changed since patch input was captured")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("patch target is not UTF-8 text") from exc
        old = str(arguments["old"])
        count = content.count(old)
        if count != 1:
            raise ToolExecutionError(f"patch fragment must occur exactly once; found {count}")
        updated = content.replace(old, str(arguments["new"]), 1)
        if sha256_digest(updated) != arguments["expected_result_hash"]:
            raise ToolExecutionError("expected result hash does not match computed patch")
        result = await self._writer.execute(
            {
                "path": arguments["path"],
                "content": updated,
                "expected_hash": arguments["expected_hash"],
            }
        )
        return {**result, "replacements": 1}

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        path = resolve_within_root(self._root, str(call.arguments["path"]))
        raw = open_readonly_no_follow(path)
        if sha256_digest(raw) != call.arguments["expected_result_hash"]:
            return None
        output = {**self._result(path, raw), "replacements": 0}
        return ToolResult(
            tool_result_id=f"reconciled-{call.tool_call_id}",
            tool_call_id=call.tool_call_id,
            success=True,
            output=output,
            output_hash=sha256_digest(str(output)),
            duration_seconds=0,
        )

    def _result(self, path: Path, content: bytes) -> dict[str, Any]:
        return {
            "path": path.relative_to(self._root.resolve()).as_posix(),
            "content_hash": sha256_digest(content),
            "bytes": len(content),
        }


class DocumentRetrieverTool(RepositoryFileReaderTool):
    """Retrieve a local document as explicitly untrusted source material."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="document.retrieve",
            description="Retrieve an approved local UTF-8 document as untrusted content",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["path", "content", "content_hash", "untrusted"],
            },
            timeout_seconds=10,
            required_permissions=frozenset({"repository.read"}),
            side_effect_class=SideEffectClass.NONE,
            retry_safe=True,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        output = dict(await super().execute(arguments))
        output["untrusted"] = True
        return output
