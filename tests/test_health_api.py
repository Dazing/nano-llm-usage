"""Health endpoints and the wire envelope (Delivery 2 Step 3, spec D6).

Distinguishing scenarios: liveness answers whenever the loop serves, readiness
is 503 until the writer finished startup (and during storage/shutdown failures),
a Jarvis outage keeps readiness 200, and every response — success or error —
carries schema_version, a UTC generated_at, and Cache-Control: no-store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asgi_client import request, with_lifespan

from jarvis_dashboard.app import (
    SCHEMA_VERSION,
    AppState,
    NoStoreMiddleware,
    create_app,
    create_asgi_app,
)
from jarvis_dashboard.config import Config
from jarvis_dashboard.domain import parse_iso
from jarvis_dashboard.lifecycle import RuntimeHealth


def make_config(tmp_path: Path) -> Config:
    return Config(
        "http://127.0.0.1:8000",
        "s1",
        15,
        3.0,
        str(tmp_path / "d.sqlite3"),
        30,
        8080,
        "INFO",
    )


def make_app(tmp_path: Path) -> tuple[NoStoreMiddleware, RuntimeHealth]:
    health = RuntimeHealth(poll_interval_seconds=15)
    config = make_config(tmp_path)
    state = AppState(config=config, health=health, db_path=Path(config.database_path))
    return create_asgi_app(state), health


def now() -> datetime:
    return datetime.now(tz=UTC)


def test_live_answers_before_startup_and_reports_no_storage_state(tmp_path):
    app, _ = make_app(tmp_path)
    response = request(app, "GET", "/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["schema_version"] == SCHEMA_VERSION
    assert "storage_status" not in body
    assert response.headers["cache-control"] == "no-store"
    assert parse_iso(body["generated_at"]).tzinfo is not None


def test_ready_is_503_until_startup_completes(tmp_path):
    app, _ = make_app(tmp_path)
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "starting"
    assert body["storage_status"] == "starting"
    assert body["collector_status"] == "starting"
    assert body["schema_version"] == SCHEMA_VERSION
    assert response.headers["cache-control"] == "no-store"


def test_ready_after_the_writer_publishes_readiness(tmp_path):
    app, health = make_app(tmp_path)
    health.mark_started(now())
    health.mark_writer_ready()
    health.mark_primary_success(now())
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["storage_status"] == "ok"
    assert body["collector_status"] == "healthy"
    assert "error" not in body


def test_jarvis_outage_keeps_ready_at_200(tmp_path):
    app, health = make_app(tmp_path)
    at = now()
    health.mark_started(at)
    health.mark_writer_ready()
    health.mark_primary_success(at)
    health.mark_attempt(at, "connection_failed")
    health.mark_primary_failure("connection_failed")
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["collector_status"] == "degraded"


def test_stale_primary_success_reports_offline_but_stays_ready(tmp_path):
    app, health = make_app(tmp_path)
    at = now()
    health.mark_started(at - timedelta(minutes=5))
    health.mark_writer_ready()
    health.mark_primary_success(at - timedelta(minutes=5))
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 200
    assert response.json()["collector_status"] == "offline"


def test_storage_failure_fails_readiness(tmp_path):
    app, health = make_app(tmp_path)
    health.mark_started(now())
    health.mark_writer_ready()
    health.mark_storage_failed("storage_read_only")
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "storage_read_only"
    assert body["storage_status"] == "unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_shutdown_disables_readiness(tmp_path):
    app, health = make_app(tmp_path)
    health.mark_started(now())
    health.mark_writer_ready()
    health.begin_shutdown()
    response = request(app, "GET", "/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "shutting_down"


def test_unknown_path_returns_structured_404(tmp_path):
    app, _ = make_app(tmp_path)
    response = request(app, "GET", "/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["error"]["code"] == "not_found"
    assert "generated_at" in body
    assert response.headers["cache-control"] == "no-store"


def test_method_not_found_is_structured_405(tmp_path):
    app, _ = make_app(tmp_path)
    response = request(app, "POST", "/health/live")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_unexpected_failure_is_payload_free_500(tmp_path):
    health = RuntimeHealth(poll_interval_seconds=15)
    config = make_config(tmp_path)
    state = AppState(config=config, health=health, db_path=Path(config.database_path))
    app = create_app(state)

    @app.get("/api/v1/boom")
    def boom() -> None:
        raise RuntimeError("secret upstream payload")

    response = request(
        NoStoreMiddleware(app), "GET", "/api/v1/boom", swallow_server_exception=True
    )
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "internal server error",
    }
    assert "secret" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_openapi_documents_the_health_contract(tmp_path):
    app, _ = make_app(tmp_path)
    response = request(app, "GET", "/api/v1/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert "/health/live" in spec["paths"]
    assert "/health/ready" in spec["paths"]
    schemas = spec["components"]["schemas"]
    for name in ("LivenessResponse", "ReadinessResponse", "ReadinessErrorResponse"):
        assert {"schema_version", "generated_at"} <= set(schemas[name]["properties"]), name
    assert "error" in schemas["ReadinessErrorResponse"]["properties"]


def test_no_wildcard_cors_is_configured(tmp_path):
    health = RuntimeHealth(poll_interval_seconds=15)
    config = make_config(tmp_path)
    state = AppState(config=config, health=health, db_path=Path(config.database_path))
    middleware = [m.cls.__name__ for m in create_app(state).user_middleware]
    assert not any("CORS" in name for name in middleware)
    assert isinstance(create_asgi_app(state), NoStoreMiddleware)


def test_lifespan_starts_and_stops_cleanly(tmp_path):
    app, _ = make_app(tmp_path)

    async def work() -> None:
        response = await request_call(app)
        assert response.status_code == 200

    asyncio.run(with_lifespan(app, work))


async def request_call(app):
    from asgi_client import call

    return await call(app, "GET", "/health/live")
