# syntax=docker/dockerfile:1.7
# Scruple API image: slim, non-root, dependencies installed by uv from uv.lock.
#   docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t scruple .
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so editing the source does not re-download them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY scruple ./scruple
COPY agent_gate ./agent_gate
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim
ARG BUILD_COMMIT=unknown
ARG BUILD_DATE=unknown
RUN groupadd --system --gid 10001 scruple \
 && useradd --system --uid 10001 --gid scruple --home-dir /app --shell /usr/sbin/nologin scruple \
 && mkdir /data && chown scruple:scruple /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/scruple-entrypoint
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCRUPLE_BUILD_COMMIT=${BUILD_COMMIT} \
    SCRUPLE_BUILD_DATE=${BUILD_DATE} \
    SCRUPLE_DATABASE_URL=/data/scruple.db \
    SCRUPLE_LOG_FORMAT=json
USER scruple:scruple
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
ENTRYPOINT ["scruple-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
