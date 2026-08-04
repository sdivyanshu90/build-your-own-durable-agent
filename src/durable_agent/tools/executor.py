"""Permission, schema, intent, idempotency, output, and recovery enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jsonschema
from pydantic import Field

from durable_agent.domain.base import DomainModel, canonical_json, sha256_digest
from durable_agent.domain.enums import SideEffectClass, ToolCallStatus
from durable_agent.domain.errors import (
    DurableAgentError,
    ManualReviewRequiredError,
    SecurityPolicyError,
    ToolExecutionError,
)
from durable_agent.domain.models import ToolCall, ToolResult
from durable_agent.domain.protocols import Clock, IdentifierGenerator
from durable_agent.observability import METRICS, get_logger, span
from durable_agent.security.redaction import SecretRedactor
from durable_agent.tools.journal import ToolJournal
from durable_agent.tools.registry import ToolRegistry


class ToolPolicy(DomainModel):
    """Effective permissions for one application/owner boundary."""

    allowed_permissions: frozenset[str] = frozenset({"repository.read", "artifact.write"})
    approved_high_impact_tools: frozenset[str] = frozenset()
    maximum_output_bytes: int = Field(default=1_000_000, ge=1_024)


class ToolExecutor:
    """Persist intent before executing any tool and cache idempotent results."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        journal: ToolJournal,
        policy: ToolPolicy,
        identifiers: IdentifierGenerator,
        clock: Clock,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._policy = policy
        self._ids = identifiers
        self._clock = clock
        self._redactor = redactor or SecretRedactor()
        self._log = get_logger("durable_agent.tools")

    async def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        """Validate, journal, execute, bound, redact, and persist a call result."""
        tool = self._registry.get(tool_name)
        definition = tool.definition
        missing = definition.required_permissions - self._policy.allowed_permissions
        if missing:
            raise SecurityPolicyError(
                f"tool {tool_name} requires denied permissions: {sorted(missing)}"
            )
        if (
            definition.side_effect_class == SideEffectClass.NON_IDEMPOTENT
            and tool_name not in self._policy.approved_high_impact_tools
        ):
            raise SecurityPolicyError(f"high-impact tool requires explicit approval: {tool_name}")
        self._validate_schema(definition.input_schema, dict(arguments), "input")
        arguments_hash = sha256_digest(canonical_json(dict(arguments)))
        existing = await self._journal.get_by_idempotency_key(idempotency_key)
        retrying = False
        if existing is not None:
            call, result = existing
            if call.tool_name != tool_name or call.arguments_hash != arguments_hash:
                raise SecurityPolicyError("tool idempotency key was reused for different input")
            if result is not None:
                return result
            if call.status == ToolCallStatus.FAILED and definition.retry_safe:
                await self._journal.prepare_retry(call.tool_call_id)
                retrying = True
            else:
                raise ManualReviewRequiredError(
                    f"tool call {call.tool_call_id} has an uncertain result; reconcile before retry"
                )
        else:
            call = ToolCall(
                tool_call_id=self._ids.new("toolcall"),
                run_id=run_id,
                task_id=task_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                arguments_hash=arguments_hash,
                idempotency_key=idempotency_key,
                side_effect_class=definition.side_effect_class,
                created_at=self._clock.now(),
            )
            await self._journal.record_intent(call)
        await self._journal.update_status(call.tool_call_id, ToolCallStatus.RUNNING)
        self._log.info(
            "tool.started",
            event_type="tool.started",
            run_id=run_id,
            task_id=task_id,
            tool_call_id=call.tool_call_id,
            tool_name=tool_name,
            retrying=retrying,
        )
        started = self._clock.now()
        try:
            with span(
                "tool.execute",
                {"run.id": run_id, "task.id": task_id, "tool.name": tool_name},
            ):
                raw_output = dict(await tool.execute(arguments))
            self._validate_schema(definition.output_schema, raw_output, "output")
            output, truncated = self._bound_output(self._redactor.redact(raw_output))
            success = True
        except Exception as exc:
            METRICS.tool_failures.labels(tool=tool_name, category=type(exc).__name__).inc()
            await self._journal.update_status(call.tool_call_id, ToolCallStatus.FAILED)
            self._log.warning(
                "tool.failed",
                event_type="tool.failed",
                run_id=run_id,
                task_id=task_id,
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, DurableAgentError):
                raise
            raise ToolExecutionError(f"tool {tool_name} failed: {exc}") from exc
        result = ToolResult(
            tool_result_id=self._ids.new("toolresult"),
            tool_call_id=call.tool_call_id,
            success=success,
            output=output,
            output_hash=sha256_digest(canonical_json(output)),
            exit_code=output.get("exit_code") if isinstance(output.get("exit_code"), int) else None,
            truncated=truncated,
            duration_seconds=max((self._clock.now() - started).total_seconds(), 0),
            created_at=self._clock.now(),
        )
        await self._journal.record_result(result)
        METRICS.tool_latency.labels(tool=tool_name).observe(result.duration_seconds)
        self._log.info(
            "tool.succeeded",
            event_type="tool.succeeded",
            run_id=run_id,
            task_id=task_id,
            tool_call_id=call.tool_call_id,
            tool_result_id=result.tool_result_id,
            tool_name=tool_name,
        )
        return result

    async def reconcile_uncertain(self, run_id: str) -> tuple[ToolResult, ...]:
        """Reconcile uncertain calls; never blindly replay non-idempotent effects."""
        reconciled = []
        for call in await self._journal.uncertain_calls(run_id):
            tool = self._registry.get(call.tool_name)
            result = await tool.reconcile(call)
            if result is not None:
                await self._journal.record_result(result)
                reconciled.append(result)
            elif call.side_effect_class == SideEffectClass.NON_IDEMPOTENT:
                await self._journal.update_status(call.tool_call_id, ToolCallStatus.NEEDS_REVIEW)
                raise ManualReviewRequiredError(
                    f"non-idempotent call {call.tool_call_id} cannot be reconciled"
                )
            elif not tool.definition.retry_safe:
                await self._journal.update_status(call.tool_call_id, ToolCallStatus.NEEDS_REVIEW)
                raise ManualReviewRequiredError(
                    f"non-retry-safe call {call.tool_call_id} cannot be reconciled"
                )
            else:
                await self._journal.update_status(call.tool_call_id, ToolCallStatus.FAILED)
        return tuple(reconciled)

    @staticmethod
    def _validate_schema(schema: dict[str, Any], value: dict[str, Any], kind: str) -> None:
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            validator_cls(schema).validate(value)
        except jsonschema.exceptions.SchemaError as exc:
            raise ToolExecutionError(f"invalid tool {kind} schema: {exc.message}") from exc
        except jsonschema.exceptions.ValidationError as exc:
            raise ToolExecutionError(f"tool {kind} validation failed: {exc.message}") from exc

    def _bound_output(self, value: Any) -> tuple[dict[str, Any], bool]:
        if not isinstance(value, dict):
            value = {"value": value}
        serialized = canonical_json(value)
        if len(serialized) <= self._policy.maximum_output_bytes:
            return value, False
        preview = serialized[: self._policy.maximum_output_bytes].decode("utf-8", errors="replace")
        return {
            "truncated": True,
            "original_bytes": len(serialized),
            "preview": preview,
        }, True
