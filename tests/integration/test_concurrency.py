from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durable_agent.domain.base import sha256_digest
from durable_agent.domain.checkpoint import CheckpointEnvelope, CheckpointPayload
from durable_agent.domain.enums import RunState
from durable_agent.domain.errors import ConcurrencyConflictError
from durable_agent.domain.models import RunRecord
from durable_agent.persistence import Database, SqlStore
from durable_agent.providers.fakes import DeterministicClock, DeterministicIdentifiers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_workers_and_concurrent_checkpoint_writers_are_fenced(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'concurrency.db'}")
    await database.create_schema_for_tests()
    clock = DeterministicClock()
    store = SqlStore(database, identifiers=DeterministicIdentifiers(), clock=clock)
    run = RunRecord(
        run_id="run",
        owner_id="owner",
        objective="exercise concurrency",
        configuration_fingerprint="f" * 64,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.create_run(run, idempotency_key="create", request_hash=sha256_digest("run"))
    outcomes = await asyncio.gather(
        store.acquire_lease("run", "worker-a", ttl_seconds=10),
        store.acquire_lease("run", "worker-b", ttl_seconds=10),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ConcurrencyConflictError) for item in outcomes) == 1

    payload = CheckpointPayload(
        run_id="run",
        run_state=RunState.RUNNING,
        active_task_id=None,
        task_states={},
        completed_task_ids=(),
        pending_task_ids=(),
        plan_id="plan",
        plan_version=1,
        configuration_fingerprint="f" * 64,
    )
    first = CheckpointEnvelope.create(
        checkpoint_id="cp-a", sequence=1, payload=payload, created_at=clock.now()
    )
    second = CheckpointEnvelope.create(
        checkpoint_id="cp-b", sequence=1, payload=payload, created_at=clock.now()
    )
    checkpoint_outcomes = await asyncio.gather(
        store.append_checkpoint(first, expected_sequence=0),
        store.append_checkpoint(second, expected_sequence=0),
        return_exceptions=True,
    )
    assert sum(item is None for item in checkpoint_outcomes) == 1
    assert sum(isinstance(item, ConcurrencyConflictError) for item in checkpoint_outcomes) == 1

    event_ids = await asyncio.gather(
        *(
            store.publish(
                run_id="run",
                event_type="concurrency.probe",
                payload={"worker": index},
            )
            for index in range(20)
        )
    )
    assert len(set(event_ids)) == 20
    events = [event async for event in store.stream("run")]
    assert [event["sequence"] for event in events] == list(range(1, 21))
    assert {event["payload"]["worker"] for event in events} == set(range(20))
    await database.dispose()
