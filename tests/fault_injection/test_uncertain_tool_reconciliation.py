from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.domain.base import canonical_json, sha256_digest
from durable_agent.domain.enums import SideEffectClass, ToolCallStatus
from durable_agent.domain.models import RunRecord, ToolCall
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers
from durable_agent.tools import ToolExecutor, ToolPolicy, ToolRegistry
from durable_agent.tools.filesystem import ControlledFileWriterTool


@pytest.mark.asyncio
async def test_crash_after_intent_reconciles_write_without_duplicate_effect(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    target = repository / "state.txt"
    target.write_text("before")
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    ids = DeterministicIdentifiers()
    store = SqlStore(database, identifiers=ids, clock=clock)
    run = RunRecord(
        run_id="run",
        owner_id="owner",
        objective="write safely",
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="create", request_hash="a" * 64)
    arguments = {
        "path": "state.txt",
        "content": "after",
        "expected_hash": sha256_digest("before"),
    }
    call = ToolCall(
        tool_call_id="call",
        run_id="run",
        task_id="task",
        tool_name="repository.write",
        arguments=arguments,
        arguments_hash=sha256_digest(canonical_json(arguments)),
        idempotency_key="write-key",
        side_effect_class=SideEffectClass.IDEMPOTENT,
        created_at=clock.now(),
    )
    await store.record_intent(call)
    # Failure injection window: the external effect happened, but result persistence did not.
    target.write_text("after")

    new_process_executor = ToolExecutor(
        registry=ToolRegistry([ControlledFileWriterTool(repository)]),
        journal=store,
        policy=ToolPolicy(allowed_permissions=frozenset({"repository.write"})),
        identifiers=ids,
        clock=clock,
    )
    reconciled = await new_process_executor.reconcile_uncertain("run")
    assert len(reconciled) == 1
    assert target.read_text() == "after"
    loaded = await store.get_by_idempotency_key("write-key")
    assert loaded is not None
    assert loaded[0].status == ToolCallStatus.SUCCEEDED
    assert loaded[1] is not None
    assert loaded[1].success
    await database.dispose()
