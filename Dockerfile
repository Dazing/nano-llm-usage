# Backend-only image: collector writer plus the bounded read API.
#
# Pinned digests (read from the public registries on 2026-10-25):
#   ghcr.io/astral-sh/uv:0.13.5   sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff
#   python:3.13.17-slim (index)   sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
#   python:3.13.17-slim (amd64)   sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18
# No Node, no build tools in the runtime stage, no docker socket, no GPU, no
# privileged mode, no dev/reload server.

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS builder

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

# Numeric, non-root owner. Pass the same UID/GID you chown the appdata
# directory to (see docs/deployment.md); compose forwards both args.
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
