# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# libolm-dev + gcc are needed to build python-olm (matrix-nio[e2e]), same as
# jellyseerr-matrix-bot's Dockerfile. Purged again after the dependency sync.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libolm-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

ADD . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN apt-get purge -y gcc && apt-get autoremove -y

RUN useradd -r bot && mkdir -p /data/store && chown -R bot:bot /data
USER bot

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT []
CMD ["python", "-u", "bot.py"]
