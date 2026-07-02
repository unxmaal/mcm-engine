# mcm-engine deployable image (MCM2-21).
#
# Two-stage build:
#   1. builder — installs the package + every adapter extra into a venv.
#   2. runtime — copies the venv onto a slim base; exposes /healthz, /readyz.
#
# Default command runs `mcm-engine serve` over HTTP/SSE on 0.0.0.0:8080.
# Override via CMD or arguments — for example `docker run ... mcm-engine
# migrate --from sqlite:///data/x.db --to postgresql://...`.

ARG PYTHON_VERSION=3.11-slim-bookworm

# ---------- builder ----------
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build deps for psycopg's binary wheel + any C deps watchdog needs.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Self-contained venv that the runtime stage will copy verbatim.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install '.[postgres,redis,opensearch]'

# ---------- runtime ----------
FROM python:${PYTHON_VERSION} AS runtime

# libpq runtime only — no build toolchain in the final image.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
        libpq5 \
        curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /bin/bash mcm

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MCM_DATA_DIR=/data

WORKDIR /home/mcm
USER mcm
RUN mkdir -p /home/mcm/.claude /home/mcm/rules

EXPOSE 8080

# Configurable bind + project via env, with sane defaults for container
# ops. Override MCM_PROJECT_NAME in deployment manifests to brand the
# server identity to the actual project.
# MCM_ALLOWED_HOSTS: comma/space separated Host values clients reach the
# daemon by (e.g. the published LAN IP or DNS name). Required past DNS-rebinding
# protection because a container can't auto-detect its -p published address.
# Loopback is always allowed; leave empty only for loopback-only access.
ENV MCM_HOST=0.0.0.0 \
    MCM_PORT=8080 \
    MCM_TRANSPORT=sse \
    MCM_ALLOWED_HOSTS="" \
    MCM_PROJECT_NAME=mcm-engine

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${MCM_PORT}/healthz || exit 1

CMD ["sh", "-c", "exec mcm-engine serve --host ${MCM_HOST} --port ${MCM_PORT} --transport ${MCM_TRANSPORT}"]
