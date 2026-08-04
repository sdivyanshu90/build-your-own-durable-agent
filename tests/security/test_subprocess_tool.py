from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.domain.errors import (
    OperationTimeoutError,
    SecurityPolicyError,
    ToolExecutionError,
)
from durable_agent.tools.subprocess import ShellCommandRunnerTool, TestRunnerTool


@pytest.mark.asyncio
async def test_shell_metacharacters_are_literal_arguments(tmp_path: Path) -> None:
    tool = ShellCommandRunnerTool(tmp_path, allowed_commands=("echo",))
    marker = tmp_path / "owned"
    result = await tool.execute({"argv": ["echo", "; touch owned"], "cwd": "."})
    assert result["exit_code"] == 0
    assert "; touch owned" in result["stdout"]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_shell_rejects_paths_and_non_allowlisted_commands(tmp_path: Path) -> None:
    tool = ShellCommandRunnerTool(tmp_path, allowed_commands=("echo",))
    for command in ("/bin/echo", "sh"):
        with pytest.raises(SecurityPolicyError, match="allowlisted"):
            await tool.execute({"argv": [command, "x"]})


@pytest.mark.asyncio
async def test_output_is_bounded(tmp_path: Path) -> None:
    tool = ShellCommandRunnerTool(tmp_path, allowed_commands=("python3",), maximum_output_bytes=20)
    result = await tool.execute({"argv": ["python3", "-c", "print('x' * 1000)"]})
    assert result["truncated"]
    assert len(result["stdout"].encode()) == 20


@pytest.mark.asyncio
async def test_test_runner_only_permits_pytest(tmp_path: Path) -> None:
    tool = TestRunnerTool(tmp_path, allowed_commands=("pytest", "echo"))
    with pytest.raises(SecurityPolicyError, match="only permits pytest"):
        await tool.execute({"argv": ["echo", "unsafe"]})


@pytest.mark.asyncio
async def test_shell_validates_argv_cwd_and_timeout(tmp_path: Path) -> None:
    tool = ShellCommandRunnerTool(tmp_path, allowed_commands=("python3",), timeout_seconds=0.05)
    with pytest.raises(ToolExecutionError, match="list of strings"):
        await tool.execute({"argv": "python3"})
    file_cwd = tmp_path / "file"
    file_cwd.write_text("not a directory")
    with pytest.raises(ToolExecutionError, match="not a directory"):
        await tool.execute({"argv": ["python3", "-c", "pass"], "cwd": "file"})
    with pytest.raises(OperationTimeoutError, match="timed out"):
        await tool.execute({"argv": ["python3", "-c", "import time; time.sleep(2)"], "cwd": "."})
