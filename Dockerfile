# syntax=docker/dockerfile:1
#
# traffic's Dockerfile (codcod/monolith platform/traffic), single-project: the
# build context is this repository's root.
#

#
# BUILD
#
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
# git: smppai is a git dependency
RUN apt-get update && apt-get install --no-install-recommends -y git binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --no-editable: porth installs as a regular wheel, so the runtime stage runs off a
# copied site-packages dir. Strip debug symbols from compiled extensions (asyncpg,
# aiohttp, ...): unstripped .so files run several times larger than they need to.
RUN uv sync --locked --no-dev --no-editable \
    && find .venv/lib/python3.13/site-packages -name '*.so' -exec strip -s {} +

#
# RUNTIME
#
# distroless debian13 ships Python 3.13 with no shell or package manager. Only the
# venv's site-packages are copied over; PYTHONPATH does the activating.
FROM gcr.io/distroless/python3-debian13
WORKDIR /opt/porth
COPY --from=builder /app/.venv/lib/python3.13/site-packages /opt/site-packages
COPY bin ./bin
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini .
ENV PYTHONPATH=/opt/site-packages

USER nonroot
# REST API, Kannel sendsms
EXPOSE 8080 13013

# Plain `python` (no shell), so the same image runs the one-off migrate step:
#   docker run ... porth -m alembic upgrade head
ENTRYPOINT ["python"]
CMD ["bin/porth", "config/config.toml"]
