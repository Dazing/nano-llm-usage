# Self-contained image: collector writer, the bounded read API, and the built
# dashboard served from the same origin.
#
# Pinned digests (read from the public registries on 2026-10-25):
#   ghcr.io/astral-sh/uv:0.13.5   sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff
#     (scratch rootfs: /uv and /uvx only — no shell, no Python)
#   python:3.13.17-slim (index)   sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
#   python:3.13.17-slim (amd64)   sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18
#   node:24.22.1 (index)          sha256:6449e19d8184d84062525b6a7524def03146c2cb04a7f08b076680d0e8e4240a
#   node:24.22.1 (amd64)          sha256:f9b3548b20b15b705b66aeecaeac7dd690c73d5e4661357aac02e60e3d38c88a
# Node exists only in the build stage: no Node, no build tools in the runtime
# stage, no docker socket, no GPU, no privileged mode, no dev/reload server.

# Frontend build stage — Node exists only here. The image builds its own assets
# (DASHBOARD_STATIC_OUT) so a stale local build can never be shipped.
FROM node:24.22.1@sha256:6449e19d8184d84062525b6a7524def03146c2cb04a7f08b076680d0e8e4240a AS frontend
WORKDIR /fe
ENV DASHBOARD_STATIC_OUT=/fe/static
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run verify && ls static/index.html static/assets

# uv's plain tag is a scratch image: /uv and /uvx and nothing else. It has no
# /bin/sh, so no shell-form RUN can execute in a stage built FROM it — it is a
# binary source only, copied into the builder below (uv's own documented pattern).
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# The builder is the runtime's own base image (same digest): a shell, and
# Python 3.13 at /usr/local/bin/python3.13 — the interpreter the /opt/venv
# copied into the runtime stage points at.
FROM python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS builder
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# The lockfile decides the environment; nothing is resolved at runtime.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Fail the build if migrations did not travel with the installed package.
RUN /opt/venv/bin/python -c "from jarvis_dashboard import storage; \
numbers = [number for number, _ in storage._migration_files()]; \
assert numbers and max(numbers) >= 2, numbers; \
print('migration numbers in image:', numbers)"

FROM python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS runtime

# Numeric, non-root owner baked as the image default. The appdata owner is an
# operator value, so compose.yaml sets the *runtime* user from JARVIS_UID/GID
# instead of passing build args: any numeric identity can run this image
# unchanged (see docs/deployment.md, "Runtime user").
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
       --shell /usr/sbin/nologin app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH= \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
    DATABASE_PATH=/data/dashboard.sqlite3

# Root-owned, read-only image content; /data is the only writable path.
COPY --from=builder /opt/venv /opt/venv
# The dashboard build, baked in (not operator-configured): STATIC_DIR points at
# it, and the API keeps serving /api/v1 and /health unchanged.
COPY --from=frontend /fe/static /opt/static
ENV STATIC_DIR=/opt/static
COPY scripts/container_healthcheck.py /usr/local/bin/container-healthcheck
RUN chmod 0555 /usr/local/bin/container-healthcheck

USER ${APP_UID}:${APP_GID}
WORKDIR /data
EXPOSE 8080

# Readiness probe using the standard library only (no curl, no wget).
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["container-healthcheck"]

ENTRYPOINT ["jarvis-dashboard"]
CMD ["serve"]
