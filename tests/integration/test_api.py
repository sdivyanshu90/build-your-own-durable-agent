from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from durable_agent.api import create_app
from durable_agent.application import build_application
from durable_agent.configuration import Settings


def test_production_api_rejects_anonymous_configuration(tmp_path: Path) -> None:
    configured = Settings(
        environment="production",
        database_url="postgresql+asyncpg://db/agent",
        sync_database_url="postgresql+psycopg://db/agent",
        repository_root=tmp_path,
    )
    with pytest.raises(ValueError, match="requires authentication"):
        create_app(settings=configured)


def api_settings(tmp_path: Path, repository: Path) -> Settings:
    database_path = tmp_path / "api.db"
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sync_database_url=f"sqlite:///{database_path}",
        repository_root=repository,
        artifact_directory=tmp_path / "artifacts",
        metrics_enabled=False,
        require_api_authentication=True,
        api_auth_token="secret-token",
        api_allowed_owners=("alice",),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_health_crud_idempotency_and_owner_authorization(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "repo"
    shutil.copytree(fixture, repository)
    settings = api_settings(tmp_path, repository)
    container = await build_application(settings, create_schema_for_tests=True)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Authorization": "Bearer secret-token",
        "X-Owner-ID": "alice",
        "Idempotency-Key": "create-key",
    }
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            assert (await client.get("/health")).json() == {"status": "healthy"}
            assert (await client.get("/ready")).status_code == 200
            unauthorized = await client.get("/v1/runs")
            assert unauthorized.status_code == 401
            created = await client.post(
                "/v1/runs",
                headers=headers,
                json={"objective": "Inspect the retry behavior", "auto_advance": False},
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["run_id"]
            replay = await client.post(
                "/v1/runs",
                headers=headers,
                json={"objective": "Inspect the retry behavior", "auto_advance": False},
            )
            assert replay.status_code == 201
            assert replay.json()["run_id"] == run_id
            conflict = await client.post(
                "/v1/runs",
                headers=headers,
                json={"objective": "A different objective", "auto_advance": False},
            )
            assert conflict.status_code == 409
            run_response = await client.get(f"/v1/runs/{run_id}", headers=headers)
            assert run_response.status_code == 200
            assert run_response.json()["owner_id"] == "alice"
            assert len((await client.get(f"/v1/runs/{run_id}/tasks", headers=headers)).json()) == 5
            assert (await client.get(f"/v1/runs/{run_id}/plan", headers=headers)).status_code == 200
            assert (
                await client.get(f"/v1/runs/{run_id}/checkpoints", headers=headers)
            ).status_code == 200
    finally:
        await container.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_pause_is_idempotent_and_owner_isolation_is_enforced(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_service"
    repository = tmp_path / "repo"
    shutil.copytree(fixture, repository)
    settings = api_settings(tmp_path, repository)
    container = await build_application(settings, create_schema_for_tests=True)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    alice = {
        "Authorization": "Bearer secret-token",
        "X-Owner-ID": "alice",
        "Idempotency-Key": "create-key",
    }
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            created = await client.post(
                "/v1/runs", headers=alice, json={"objective": "Inspect safely"}
            )
            run_id = created.json()["run_id"]
            forbidden = await client.get(
                f"/v1/runs/{run_id}",
                headers={
                    "Authorization": "Bearer secret-token",
                    "X-Owner-ID": "mallory",
                },
            )
            assert forbidden.status_code == 403
            pause_headers = {**alice, "Idempotency-Key": "pause-key"}
            first = await client.post(
                f"/v1/runs/{run_id}/pause",
                headers=pause_headers,
                json={"reason": "maintenance"},
            )
            assert first.status_code == 200, first.text
            assert (await client.get(f"/v1/runs/{run_id}", headers=alice)).json()[
                "state"
            ] == "PAUSED"
            # The accepted request is replayed even though the run has since paused.
            second = await client.post(
                f"/v1/runs/{run_id}/pause",
                headers=pause_headers,
                json={"reason": "maintenance"},
            )
            assert second.status_code == 200
            assert second.json()["request_id"] == first.json()["request_id"]
            resumed, replayed_resume = await asyncio.gather(
                client.post(
                    f"/v1/runs/{run_id}/resume",
                    headers={**alice, "Idempotency-Key": "resume-key"},
                    json={"maximum_tasks": 0},
                ),
                client.post(
                    f"/v1/runs/{run_id}/resume",
                    headers={**alice, "Idempotency-Key": "resume-key"},
                    json={"maximum_tasks": 0},
                ),
            )
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["state"] == "RUNNING"
            assert replayed_resume.status_code == 200
            assert replayed_resume.json()["state"] in {"PAUSED", "RECOVERING", "RUNNING"}
            resume_conflict = await client.post(
                f"/v1/runs/{run_id}/resume",
                headers={**alice, "Idempotency-Key": "resume-key"},
                json={"maximum_tasks": 1},
            )
            assert resume_conflict.status_code == 409
            cancelled = await client.post(
                f"/v1/runs/{run_id}/cancel",
                headers={**alice, "Idempotency-Key": "cancel-key"},
                json={"reason": "test finished"},
            )
            assert cancelled.status_code == 200, cancelled.text
            assert (await client.get(f"/v1/runs/{run_id}", headers=alice)).json()[
                "state"
            ] == "CANCELLED"
            evidence = await client.get(f"/v1/runs/{run_id}/evidence", headers=alice)
            assert evidence.status_code == 200
            report = await client.get(f"/v1/runs/{run_id}/report?format=json", headers=alice)
            assert report.status_code == 200
            assert report.json()["partial"] is True
    finally:
        await container.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_text(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    settings = api_settings(tmp_path, repository).model_copy(update={"metrics_enabled": True})
    container = await build_application(settings, create_schema_for_tests=True)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            response = await client.get("/metrics/")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")
            assert "durable_agent_runs_created_total" in response.text
            assert "durable_agent_checkpoint_writes_total" in response.text
    finally:
        await container.close()
