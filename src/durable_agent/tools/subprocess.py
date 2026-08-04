"""Argument-array subprocess and test tools with time/output/environment controls."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from durable_agent.domain.enums import SideEffectClass
from durable_agent.domain.errors import (
    OperationTimeoutError,
    SecurityPolicyError,
    ToolExecutionError,
)
from durable_agent.domain.models import ToolCall, ToolDefinition, ToolResult
from durable_agent.security.paths import resolve_within_root


class ShellCommandRunnerTool:
    """Execute allowlisted argv with `shell=False` and a sanitized environment."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_commands: Sequence[str],
        allowed_environment_variables: Sequence[str] = ("PATH", "LANG", "LC_ALL"),
        timeout_seconds: float = 60,
        maximum_output_bytes: int = 1_000_000,
    ) -> None:
        self._root = root
        self._allowed_commands = frozenset(allowed_commands)
        self._allowed_environment_variables = frozenset(allowed_environment_variables)
        self._timeout = timeout_seconds
        self._maximum_output = maximum_output_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shell.run",
            description="Run an allowlisted executable with an argument array",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "cwd": {"type": "string"},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["argv", "exit_code", "stdout", "stderr", "truncated"],
            },
            timeout_seconds=self._timeout,
            required_permissions=frozenset({"process.execute"}),
            side_effect_class=SideEffectClass.REVERSIBLE,
            retry_safe=False,
            produces_evidence=True,
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raw_argv = arguments["argv"]
        if not isinstance(raw_argv, list) or not all(isinstance(item, str) for item in raw_argv):
            raise ToolExecutionError("argv must be a list of strings")
        argv = tuple(raw_argv)
        executable = Path(argv[0]).name
        if executable != argv[0] or executable not in self._allowed_commands:
            raise SecurityPolicyError(f"executable is not allowlisted: {argv[0]}")
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise ToolExecutionError(f"executable was not found: {executable}")
        cwd_value = str(arguments.get("cwd", "."))
        cwd = resolve_within_root(self._root, cwd_value, allow_root=True)
        if not cwd.is_dir():
            raise ToolExecutionError("command working directory is not a directory")
        environment = self._environment()
        process = await asyncio.create_subprocess_exec(
            resolved_executable,
            *argv[1:],
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        output_task = asyncio.gather(stdout_task, stderr_task)
        try:
            # EOF on both pipes proves the child closed its output descriptors. Some
            # constrained child watchers can delay Process.wait() notification even
            # after returncode is available, so pipe completion is the primary signal.
            stdout, stderr = await asyncio.wait_for(output_task, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            await self._terminate(process, output_task)
            raise OperationTimeoutError(f"command timed out after {self._timeout} seconds") from exc
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except asyncio.TimeoutError as exc:
                # Cancellation can flush a delayed child-watcher notification. Accept
                # that notification only when the transport now exposes an exit code.
                if process.returncode is None:
                    await self._terminate(process, output_task)
                    raise OperationTimeoutError(
                        f"command exit status was unavailable after {self._timeout} seconds"
                    ) from exc
        if process.returncode is None:
            raise ToolExecutionError("command exited without an observable status")
        return {
            "argv": list(argv),
            "exit_code": process.returncode,
            "stdout": stdout[0].decode("utf-8", errors="replace"),
            "stderr": stderr[0].decode("utf-8", errors="replace"),
            "truncated": stdout[1] or stderr[1],
        }

    @staticmethod
    async def _terminate(
        process: asyncio.subprocess.Process,
        output_task: asyncio.Future[Any],
    ) -> None:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
        if not output_task.done():
            output_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await output_task

    async def reconcile(self, call: ToolCall) -> ToolResult | None:
        del call
        return None

    async def _read_bounded(self, stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        chunks: list[bytes] = []
        stored = 0
        truncated = False
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                break
            remaining = self._maximum_output - stored
            if remaining > 0:
                chunks.append(chunk[:remaining])
                stored += min(len(chunk), remaining)
            if len(chunk) > remaining:
                truncated = True
        return b"".join(chunks), truncated

    def _environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key in self._allowed_environment_variables
        }


class TestRunnerTool(ShellCommandRunnerTool):
    """Restricted pytest runner with stable output schema."""

    __test__ = False

    @property
    def definition(self) -> ToolDefinition:
        return self._test_definition()

    def _test_definition(self) -> ToolDefinition:
        base = super().definition
        return base.model_copy(
            update={
                "name": "test.run",
                "description": "Run pytest with explicit arguments inside the repository",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "cwd": {"type": "string"},
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
            }
        )

    async def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        argv = arguments.get("argv")
        direct = isinstance(argv, list) and bool(argv) and Path(str(argv[0])).name == "pytest"
        module = (
            isinstance(argv, list)
            and len(argv) >= 3
            and Path(str(argv[0])).name in {"python", "python3"}
            and argv[1:3] == ["-m", "pytest"]
        )
        if not direct and not module:
            raise SecurityPolicyError("test runner only permits pytest")
        return await super().execute(arguments)

    def _environment(self) -> dict[str, str]:
        environment = super()._environment()
        # Third-party pytest auto-loaded plugins can start network clients or background
        # threads. Project tests declare needed plugins explicitly in their environment.
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        return environment
