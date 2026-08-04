"""Low-cardinality Prometheus-compatible metric instruments."""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter, Histogram


@dataclass(frozen=True)
class Metrics:
    runs_created: Counter
    runs_completed: Counter
    runs_failed: Counter
    runs_paused: Counter
    task_duration: Histogram
    task_retries: Counter
    tool_latency: Histogram
    tool_failures: Counter
    checkpoint_writes: Counter
    checkpoint_recoveries: Counter
    context_compressions: Counter
    token_estimates: Counter
    retrievals: Counter
    evidence_records: Counter
    reports_generated: Counter


METRICS = Metrics(
    runs_created=Counter("durable_agent_runs_created_total", "Runs created"),
    runs_completed=Counter("durable_agent_runs_completed_total", "Runs completed"),
    runs_failed=Counter("durable_agent_runs_failed_total", "Runs failed"),
    runs_paused=Counter("durable_agent_runs_paused_total", "Runs paused"),
    task_duration=Histogram("durable_agent_task_duration_seconds", "Task duration", ("task_kind",)),
    task_retries=Counter("durable_agent_task_retries_total", "Task retries", ("category",)),
    tool_latency=Histogram("durable_agent_tool_latency_seconds", "Tool latency", ("tool",)),
    tool_failures=Counter(
        "durable_agent_tool_failures_total", "Tool failures", ("tool", "category")
    ),
    checkpoint_writes=Counter("durable_agent_checkpoint_writes_total", "Checkpoint writes"),
    checkpoint_recoveries=Counter(
        "durable_agent_checkpoint_recoveries_total", "Checkpoint recovery fallbacks"
    ),
    context_compressions=Counter(
        "durable_agent_context_compressions_total", "Context compressions"
    ),
    token_estimates=Counter("durable_agent_tokens_estimated_total", "Estimated context tokens"),
    retrievals=Counter("durable_agent_retrieval_items_total", "Retrieved items", ("strategy",)),
    evidence_records=Counter("durable_agent_evidence_records_total", "Evidence records", ("type",)),
    reports_generated=Counter(
        "durable_agent_reports_generated_total", "Reports generated", ("partial",)
    ),
)
