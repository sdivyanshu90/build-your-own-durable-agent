"""Tool intent/result journal protocol and deterministic implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from durable_agent.domain.enums import ToolCallStatus
from durable_agent.domain.models import ToolCall, ToolResult


class ToolJournal(Protocol):
    async def get_by_idempotency_key(
        self, key: str
    ) -> tuple[ToolCall, ToolResult | None] | None: ...

    async def record_intent(self, call: ToolCall) -> None: ...

    async def update_status(self, call_id: str, status: ToolCallStatus) -> None: ...

    async def prepare_retry(self, call_id: str) -> None: ...

    async def record_result(self, result: ToolResult) -> None: ...

    async def uncertain_calls(self, run_id: str) -> Sequence[ToolCall]: ...


class InMemoryToolJournal:
    """Deterministic journal for tests; SQL journal is used by the application."""

    def __init__(self) -> None:
        self.calls: dict[str, ToolCall] = {}
        self.results: dict[str, ToolResult] = {}
        self.keys: dict[str, str] = {}

    async def get_by_idempotency_key(self, key: str) -> tuple[ToolCall, ToolResult | None] | None:
        call_id = self.keys.get(key)
        if call_id is None:
            return None
        return self.calls[call_id], self.results.get(call_id)

    async def record_intent(self, call: ToolCall) -> None:
        if call.idempotency_key in self.keys:
            raise ValueError("duplicate tool idempotency key")
        self.calls[call.tool_call_id] = call
        self.keys[call.idempotency_key] = call.tool_call_id

    async def update_status(self, call_id: str, status: ToolCallStatus) -> None:
        self.calls[call_id] = self.calls[call_id].model_copy(update={"status": status})

    async def prepare_retry(self, call_id: str) -> None:
        call = self.calls[call_id]
        if call.status != ToolCallStatus.FAILED or call_id in self.results:
            raise ValueError("only failed tool calls without results can be retried")
        self.calls[call_id] = call.model_copy(
            update={
                "status": ToolCallStatus.INTENT_RECORDED,
                "attempt": call.attempt + 1,
                "started_at": None,
                "finished_at": None,
            }
        )

    async def record_result(self, result: ToolResult) -> None:
        self.results[result.tool_call_id] = result
        await self.update_status(
            result.tool_call_id,
            ToolCallStatus.SUCCEEDED if result.success else ToolCallStatus.FAILED,
        )

    async def uncertain_calls(self, run_id: str) -> Sequence[ToolCall]:
        return tuple(
            call
            for call in self.calls.values()
            if call.run_id == run_id
            and call.status
            in {
                ToolCallStatus.INTENT_RECORDED,
                ToolCallStatus.RUNNING,
                ToolCallStatus.UNCERTAIN,
            }
        )
