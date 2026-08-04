"""Authenticated, owner-scoped FastAPI control plane."""

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import make_asgi_app

from durable_agent.api.schemas import CreateRunRequest, LifecycleRequestBody, ResumeRequestBody
from durable_agent.application import ApplicationContainer, build_application
from durable_agent.configuration import Settings
from durable_agent.domain.errors import (
    ConcurrencyConflictError,
    DurableAgentError,
    IdempotencyConflictError,
    NotFoundError,
    SecurityPolicyError,
)
from durable_agent.observability import configure_logging


def create_app(
    container: ApplicationContainer | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    """Create the API; an injected container keeps integration tests deterministic."""
    configured = settings or (container.settings if container else Settings())
    if configured.environment == "production" and not configured.require_api_authentication:
        raise ValueError("production HTTP API requires authentication")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        active = container or await build_application(configured)
        app.state.container = active
        configure_logging(
            level=configured.log_level,
            json_output=configured.log_json,
            secrets=(
                configured.api_auth_token.get_secret_value()
                if configured.api_auth_token is not None
                else "",
            ),
        )
        try:
            yield
        finally:
            if owned:
                await active.close()

    app = FastAPI(
        title="Durable Coding and Research Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    if configured.metrics_enabled:
        app.mount("/metrics", make_asgi_app())

    @app.exception_handler(DurableAgentError)
    async def domain_error(request: Request, exc: DurableAgentError) -> JSONResponse:
        del request
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, SecurityPolicyError):
            code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, ConcurrencyConflictError | IdempotencyConflictError):
            code = status.HTTP_409_CONFLICT
        return JSONResponse(
            status_code=code,
            content={"error": exc.category.value, "message": exc.message},
        )

    async def owner(
        authorization: Annotated[str | None, Header()] = None,
        x_owner_id: Annotated[str, Header()] = "local",
    ) -> str:
        if configured.require_api_authentication:
            expected = (
                configured.api_auth_token.get_secret_value()
                if configured.api_auth_token is not None
                else ""
            )
            supplied = authorization.removeprefix("Bearer ") if authorization else ""
            if not expected or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="invalid bearer token")
        if configured.api_allowed_owners and x_owner_id not in configured.api_allowed_owners:
            raise HTTPException(status_code=403, detail="owner is not authorized")
        return x_owner_id

    async def active(request: Request) -> ApplicationContainer:
        return cast(ApplicationContainer, request.app.state.container)

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready", tags=["operations"])
    async def ready(application: Annotated[ApplicationContainer, Depends(active)]) -> JSONResponse:
        ready_value = await application.database.ping() and await application.store.schema_ready()
        return JSONResponse(
            status_code=200 if ready_value else 503,
            content={"status": "ready" if ready_value else "not_ready"},
        )

    @app.post("/v1/runs", status_code=201, tags=["runs"])
    async def create_run(
        body: CreateRunRequest,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        run = await application.service.create_run(
            objective=body.objective,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            repository_path=body.repository_path,
        )
        if body.auto_advance:
            result = await application.service.advance(run.run_id, owner_id=owner_id)
            run = result.run
        return run.model_dump(mode="json")

    @app.get("/v1/runs", tags=["runs"])
    async def list_runs(
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in await application.store.list_runs(owner_id=owner_id)
        ]

    @app.get("/v1/runs/{run_id}", tags=["runs"])
    async def get_run(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> dict[str, object]:
        return (await application.service.status(run_id, owner_id=owner_id)).model_dump(mode="json")

    @app.get("/v1/runs/{run_id}/plan", tags=["plans"])
    async def get_plan(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> dict[str, object]:
        await application.service.status(run_id, owner_id=owner_id)
        return (await application.store.get_plan(run_id)).model_dump(mode="json")

    @app.get("/v1/runs/{run_id}/tasks", tags=["plans"])
    async def get_tasks(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> list[dict[str, object]]:
        await application.service.status(run_id, owner_id=owner_id)
        return [item.model_dump(mode="json") for item in await application.store.get_tasks(run_id)]

    @app.post("/v1/runs/{run_id}/pause", tags=["lifecycle"])
    async def pause(
        run_id: str,
        body: LifecycleRequestBody,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        request = await application.service.pause(
            run_id,
            owner_id=owner_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        await application.service.advance(run_id, owner_id=owner_id)
        return request.model_dump(mode="json")

    @app.post("/v1/runs/{run_id}/resume", tags=["lifecycle"])
    async def resume(
        run_id: str,
        body: ResumeRequestBody,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return (
            await application.service.resume(
                run_id,
                owner_id=owner_id,
                maximum_tasks=body.maximum_tasks,
                idempotency_key=idempotency_key,
            )
        ).run.model_dump(mode="json")

    @app.post("/v1/runs/{run_id}/cancel", tags=["lifecycle"])
    async def cancel(
        run_id: str,
        body: LifecycleRequestBody,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        request = await application.service.cancel(
            run_id,
            owner_id=owner_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        await application.service.advance(run_id, owner_id=owner_id)
        return request.model_dump(mode="json")

    @app.get("/v1/runs/{run_id}/checkpoints", tags=["state"])
    async def checkpoints(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> list[dict[str, object]]:
        await application.service.status(run_id, owner_id=owner_id)
        return [dict(item) for item in await application.store.list_checkpoint_views(run_id)]

    @app.get("/v1/runs/{run_id}/evidence", tags=["evidence"])
    async def evidence(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
    ) -> list[dict[str, object]]:
        await application.service.status(run_id, owner_id=owner_id)
        return [
            item.model_dump(mode="json") for item in await application.store.get_evidence(run_id)
        ]

    @app.get("/v1/runs/{run_id}/report", tags=["reports"])
    async def report(
        run_id: str,
        application: Annotated[ApplicationContainer, Depends(active)],
        owner_id: Annotated[str, Depends(owner)],
        format: Annotated[str, Query(pattern="^(markdown|json)$")] = "markdown",
    ) -> PlainTextResponse:
        content = await application.service.report(run_id, owner_id=owner_id, format=format)
        return PlainTextResponse(
            content,
            media_type="application/json" if format == "json" else "text/markdown",
        )

    return app
