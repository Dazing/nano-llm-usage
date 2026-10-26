"""Minimal in-process ASGI client for tests (no extra dependency).

Starlette's TestClient blocks in this environment (it warns that `httpx` is
deprecated in favour of `httpx2`), so tests drive the ASGI app directly. This
keeps real ASGI semantics — lifespan, middleware, status codes, headers — with
no third-party transport.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from typing import Any, Awaitable, Callable

from starlette.types import ASGIApp, Scope


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return jsonlib.loads(self.body)


def _scope(method: str, path: str, query: str, headers: dict[str, str] | None) -> Scope:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "headers": raw,
    }


async def call(
    app: ASGIApp,
    method: str,
    path: str,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    swallow_server_exception: bool = False,
) -> Response:
    """One ASGI HTTP call.

    ``swallow_server_exception`` mirrors a real server: Starlette's
    ServerErrorMiddleware sends the 500 response and then re-raises so the
    server logs it. Set it when asserting on that generated response.
    """
    state: dict[str, Any] = {"status": 500, "headers": {}, "chunks": []}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
            state["headers"] = {
                key.decode(): value.decode() for key, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            state["chunks"].append(message.get("body", b""))

    started = False
    original_send = send

    async def send(message: dict) -> None:  # type: ignore[misc]
        nonlocal started
        started = started or message["type"] == "http.response.start"
        await original_send(message)

    try:
        await app(_scope(method, path, query, headers), receive, send)
    except BaseException:
        if not swallow_server_exception or not started:
            raise
    return Response(state["status"], state["headers"], b"".join(state["chunks"]))


def request(
    app: ASGIApp,
    method: str,
    path: str,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    swallow_server_exception: bool = False,
) -> Response:
    return asyncio.run(
        call(
            app,
            method,
            path,
            query=query,
            headers=headers,
            body=body,
            swallow_server_exception=swallow_server_exception,
        )
    )


def request_many(app: ASGIApp, calls: list[tuple[str, str, str]]) -> list[Response]:
    """Concurrent requests in one event loop: each call is (method, path, query)."""

    async def run() -> list[Response]:
        return await asyncio.gather(
            *(call(app, method, path, query=query) for method, path, query in calls)
        )

    return asyncio.run(run())


async def with_lifespan(app: ASGIApp, work: Callable[[], Awaitable[None]]) -> None:
    """Run `work()` between the app's lifespan startup and shutdown."""
    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}, "app": app}
    queue: asyncio.Queue[dict] = asyncio.Queue()
    started = asyncio.Event()
    stopped = asyncio.Event()
    await queue.put({"type": "lifespan.startup"})

    async def receive() -> dict:
        return await queue.get()

    async def send(message: dict) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()
        elif message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
            stopped.set()

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        await work()
    finally:
        await queue.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(stopped.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=5)
