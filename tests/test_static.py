"""The built dashboard is served beside the API, with scoped cache headers.

Distinguishing scenarios (Delivery 3 D1/D2/D3), all against `create_asgi_app` —
the ASGI entry point uvicorn runs: a build directory is served from `/` with
`no-cache` so a deploy is never hidden behind a stale index; hashed assets under
`/assets` are `immutable`; the API and health responses keep `no-store`; an
unknown API path and an unknown page path are both structured JSON 404s and stay
`no-store`; a non-GET page path keeps the API's own answer; a generic 500 keeps
`no-store` from the outermost wrapper; and with no build present the API-only
root stays a plain JSON 404.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from asgi_client import request
from test_storage_helpers import SOURCE, seed_chain

from jarvis_dashboard import storage
from jarvis_dashboard.app import AppState, NoStoreMiddleware, create_app, create_asgi_app
from jarvis_dashboard.config import Config
from jarvis_dashboard.domain import utc_now
from jarvis_dashboard.lifecycle import RuntimeHealth

INDEX = "<!doctype html><html lang=en><body><div id=app></div></body></html>\n"
ASSET = "console.log('jarvis');\n"
NO_BUILD = "no-build"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "d.sqlite3"
    conn = storage.open_storage(path)
    seed_chain(conn, "m", [(1000, 100), (1600, 140)], utc_now())
    conn.close()
    return path


@pytest.fixture
def build(tmp_path: Path) -> Path:
    static = tmp_path / "build"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX, encoding="utf-8")
    (static / "assets" / "app-abc123.js").write_text(ASSET, encoding="utf-8")
    return static


def make_health() -> RuntimeHealth:
    health = RuntimeHealth(poll_interval_seconds=15)
    health.mark_started(utc_now())
    health.mark_writer_ready()
    health.mark_primary_success(utc_now())
    return health


def config_for(db: Path, static_dir: str, **kw) -> Config:
    config = Config("http://127.0.0.1:8000", SOURCE, 15, 3.0, str(db), 30, 8080, "INFO")
    for key, value in kw.items():
        object.__setattr__(config, key, value)
    object.__setattr__(config, "static_dir", static_dir)
    return config


def served(db: Path, static_dir: str, health: RuntimeHealth | None = None):
    """The ASGI entry point: page layer outside the API, as uvicorn runs it."""
    return create_asgi_app(AppState(config_for(db, static_dir), health or make_health(), db))


def get(app, path: str, **kw):
    return request(app, "GET", path, **kw)


# --------------------------------------------------------------------------- #
def test_index_is_served_from_root_and_is_never_stale(db, build):
    response = get(served(db, str(build)), "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == INDEX


def test_hashed_assets_are_immutable(db, build):
    response = get(served(db, str(build)), "/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.text == ASSET


def test_api_and_health_responses_keep_no_store(db, build):
    app = served(db, str(build))
    status = get(app, "/api/v1/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["schema_version"] == 1
    live = get(app, "/health/live")
    assert live.status_code == 200
    assert live.headers["cache-control"] == "no-store"
    spec = get(app, "/api/v1/openapi.json")
    assert spec.status_code == 200
    assert spec.headers["cache-control"] == "no-store"


def test_an_unknown_api_path_is_a_json_404_and_no_store(db, build):
    response = get(served(db, str(build)), "/api/v1/definitely-missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["cache-control"] == "no-store"


def test_an_unknown_page_is_a_json_404_not_an_empty_shell(db, build):
    response = get(served(db, str(build)), "/missing.txt")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["cache-control"] == "no-store"


def test_a_posted_api_path_keeps_the_api_405(db, build):
    response = request(served(db, str(build)), "POST", "/health/live")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["cache-control"] == "no-store"


def test_the_page_mount_never_answers_an_api_path(db, build):
    app = served(db, str(build))
    for path in ("/api", "/api/v1/nope", "/health/nope"):
        response = get(app, path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found", path
        assert response.headers["cache-control"] == "no-store", path


def test_a_generic_500_keeps_no_store_from_the_outer_wrapper(db, build):
    app = create_app(AppState(config_for(db, str(build)), make_health(), db))

    @app.get("/api/v1/boom")
    async def boom():
        raise RuntimeError("boom")

    response = request(
        NoStoreMiddleware(app), "GET", "/api/v1/boom", swallow_server_exception=True
    )
    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"


def test_without_a_build_the_root_stays_a_plain_json_404(db, tmp_path):
    app = served(db, str(tmp_path / NO_BUILD))
    root = get(app, "/")
    assert root.status_code == 404
    assert root.json()["error"]["code"] == "not_found"
    assert root.headers["cache-control"] == "no-store"
    assert get(app, "/api/v1/status").status_code == 200


def test_a_partial_build_without_index_html_is_not_mounted(db, tmp_path):
    partial = tmp_path / "partial"
    (partial / "assets").mkdir(parents=True)
    (partial / "assets" / "app-abc123.js").write_text(ASSET, encoding="utf-8")
    app = served(db, str(partial))
    assert get(app, "/").status_code == 404
    assert get(app, "/assets/app-abc123.js").status_code == 404
