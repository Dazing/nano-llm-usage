"""HTTP application: envelope, errors, and lifecycle health (Delivery 2 Steps 3, 5).

Every response the application produces — success, error, and health — carries
``schema_version`` and a per-request UTC ``generated_at``, and is marked
``Cache-Control: no-store``. Validation problems are normalised to 400 with the
same error model; unexpected failures return a payload-free generic 500 with a
bounded log code, never a traceback.
"""

from __future__ import annotations

import logging
import uuid
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import ApiFailure, ReadAdmission, build_router
from .config import Config
from .domain import to_iso, utc_now
from .lifecycle import RuntimeHealth
from .schemas import (
    NO_STORE,
    SCHEMA_VERSION,
    Envelope,
    ErrorDetail,
    ErrorResponse,
    LivenessResponse,
    ReadinessErrorResponse,
    ReadinessResponse,
)

__all__ = [
    "AppState",
    "Envelope",
    "ErrorDetail",
    "ErrorResponse",
    "LivenessResponse",
    "NO_STORE",
    "ReadinessErrorResponse",
    "ReadinessResponse",
    "SCHEMA_VERSION",
    "create_app",
    "create_asgi_app",
]

logger = logging.getLogger("jarvis_dashboard.api")


@dataclass
class AppState:
    """Everything the app needs: configuration, lifecycle, and the volume path."""

    config: Config
    health: RuntimeHealth
    db_path: Path
    admission: ReadAdmission = field(default_factory=ReadAdmission)
    service: Any = None  # service.Service when serving; None in tests
    server: Any = None  # uvicorn server, set by service.build_server


async def _watch_writer(service: Any, server: Any) -> None:
    """Writer-thread death ends the service (plan Step 3)."""
    await asyncio.to_thread(service.wait_finished)
    if server is not None:
        server.should_exit = True
    else:  # pragma: no cover - defensive
        service.health.begin_shutdown()


def create_app(state: AppState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if state.service is not None:
            task = asyncio.create_task(
                _watch_writer(state.service, state.server)
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(

        title="Jarvis Dashboard API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.dashboard = state

    @app.middleware("http")
    async def no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Cache-Control"] = NO_STORE
        return response

    @app.get("/health/live", response_model=LivenessResponse, tags=["health"])
    def health_live() -> JSONResponse:
        """200 whenever the event loop serves; never queries storage or Jarvis."""
        body = LivenessResponse(status="alive", generated_at=to_iso(utc_now()))
        return JSONResponse(body.model_dump(), status_code=200)

    @app.get(
        "/health/ready",
        response_model=ReadinessResponse,
        tags=["health"],
        responses={
            503: {"model": ReadinessErrorResponse},
            500: {"model": ErrorResponse},
        },
    )
    def health_ready() -> JSONResponse:
        """200 only after writable storage and collection start (spec D6)."""
        snapshot = state.health.snapshot(utc_now())
        ready = snapshot.ready
        body = ReadinessResponse(
            status="ready" if ready else "unavailable",
            storage_status=snapshot.storage_status,
            storage_code=snapshot.storage_code,
            collector_status=snapshot.collector_status,
            generated_at=to_iso(utc_now()),
        )
        if ready:
            return JSONResponse(body.model_dump(), status_code=200)
        code = snapshot.storage_code or "starting"
        if snapshot.shutting_down:
            code = "shutting_down"
        error = ReadinessErrorResponse(
            status=body.status,
            generated_at=body.generated_at,
            error=ErrorDetail(
                code=code,
                message="dashboard is not ready to serve" if not snapshot.shutting_down
                else "dashboard is shutting down",
            ),
            storage_status=body.storage_status,
            storage_code=body.storage_code,
            collector_status=body.collector_status,
        )
        return JSONResponse(error.model_dump(), status_code=503)

    @app.exception_handler(ApiFailure)
    async def on_api_failure(request: Request, exc: ApiFailure) -> JSONResponse:
        return _error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, "invalid_request", "request parameters are invalid")

    @app.exception_handler(StarletteHTTPException)
    async def on_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        message = str(exc.detail)
        if exc.status_code >= 500:
            message = "internal server error"
        return _error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Bounded, payload-free logging: a correlation code, never a traceback.
        reference = uuid.uuid4().hex[:8]
        logger.error("unhandled_error reference=%s type=%s", reference, type(exc).__name__)
        return _error_response(500, "internal_error", "internal server error")

    app.include_router(build_router())
    # Request validation is normalised to the common 400 model at runtime, so the
    # frozen document must not advertise FastAPI's default 422 contract.
    app.openapi_schema = _normalise_openapi(app.openapi())
    return app


def _normalise_openapi(spec: dict[str, Any]) -> dict[str, Any]:
    for path in spec.get("paths", {}).values():
        for operation in path.values():
            responses = operation.get("responses", {})
            responses.pop("422", None)
    schemas = spec.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    return spec


def _error_response(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = ErrorResponse(
        generated_at=to_iso(utc_now()),
        error=ErrorDetail(code=code, message=message),
        **extra,
    )
    return JSONResponse(body.model_dump(), status_code=status)


def create_asgi_app(state: AppState) -> ASGIApp:
    """ASGI entry point: the no-store header must also cover the generic 500.

    Uvicorn/Starlette generate that response in ServerErrorMiddleware, which sits
    outside every in-application middleware, so the wrapper has to be outermost.
    """
    return NoStoreMiddleware(create_app(state))


class NoStoreMiddleware:
    """Outermost pure-ASGI middleware: every response is marked no-store."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = dict(message["headers"])
                headers[b"cache-control"] = NO_STORE.encode()
                message = {**message, "headers": list(headers.items())}
            await send(message)

        await self.app(scope, receive, send_wrapper)
