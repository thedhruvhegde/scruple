# syntax=docker/dockerfile:1.7
# Scruple API image: slim, non-root (uid 10001), runs with a read-only root
# filesystem, tini as PID 1, gunicorn with uvicorn workers configured from settings.
#   docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t scruple .
#   docker run --read-only --tmpfs /tmp -v scruple-data:/data -p 8000:8000 scruple
#
# Base images are pinned by multi-arch index digest (tag kept for readers). To
# refresh: docker buildx imagetools inspect python:3.12-slim (and the uv image),
# paste the "Digest:" line here, rebuild, rescan. See docs/DEPLOY.md.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv

# tini, the static release binary for the target architecture, checked against the
# sha256 published with v0.19.0 (no package manager needed in the final image).
FROM ${PYTHON_IMAGE} AS tini
ARG TARGETARCH
ARG TINI_VERSION=v0.19.0
ARG TINI_SHA256_AMD64=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
ARG TINI_SHA256_ARM64=eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-static-${TARGETARCH} /tini
RUN case "${TARGETARCH}" in \
      amd64) want="${TINI_SHA256_AMD64}" ;; \
      arm64) want="${TINI_SHA256_ARM64}" ;; \
      *) echo "no tini checksum for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && echo "${want}  /tini" | sha256sum -c - \
 && chmod 0755 /tini

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so editing the source does not re-download them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel --no-install-project
COPY scruple ./scruple
COPY agent_gate ./agent_gate
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel --no-editable

FROM ${PYTHON_IMAGE}
ARG BUILD_COMMIT=unknown
ARG BUILD_DATE=unknown
# Pick up Debian security fixes released after the base image was built.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 scruple \
 && useradd --system --uid 10001 --gid scruple --home-dir /app --shell /usr/sbin/nologin scruple \
 && mkdir /data && chown scruple:scruple /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# tini: PID 1 that forwards SIGTERM to gunicorn and reaps zombies.
COPY --from=tini /tini /usr/bin/tini
COPY docker/entrypoint.sh /usr/local/bin/scruple-entrypoint
COPY deploy/docker/gunicorn.conf.py /app/gunicorn.conf.py
# Nothing writes outside /data and /tmp: bytecode is compiled at build time,
# gunicorn's worker heartbeat files go to TMPDIR and per-worker metric samples to
# PROMETHEUS_MULTIPROC_DIR. Mount /tmp as tmpfs/emptyDir.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp \
    PROMETHEUS_MULTIPROC_DIR=/tmp/metrics \
    SCRUPLE_BUILD_COMMIT=${BUILD_COMMIT} \
    SCRUPLE_BUILD_DATE=${BUILD_DATE} \
    SCRUPLE_DATABASE_URL=/data/scruple.db \
    SCRUPLE_LOG_FORMAT=json \
    SCRUPLE_API_HOST=0.0.0.0 \
    SCRUPLE_API_PORT=8000
# Numeric uid:gid so Kubernetes runAsNonRoot can check it without /etc/passwd.
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('SCRUPLE_API_PORT', '8000'), timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--", "scruple-entrypoint"]
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py"]
