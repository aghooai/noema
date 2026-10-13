# noema-api: the api process image, also the base of noema-worker (jobs, media-worker) (ADR-002 §8, spec-01 §4).
# `uv sync --frozen --no-dev` WITHOUT the media group: no Pillow, pillow-heif, pikepdf, Docling, torch, pypdfium2, magika,
# python-magic, markdown-it-py or libmagic, so a forbidden import (spec-01 §2 rule 7) also fails at import time.
# Build from the repo root:  docker build -f deploy/docker/api.Dockerfile -t noema-api:dev .
# The context is filtered by api.Dockerfile.dockerignore (an allowlist).
#
# Interpreter (Wave F; sizing risk 1 in reports/perf/sizing.md): CPython from python-build-standalone, installed by uv
# (`uv python install`, checksum-verified against uv's pinned download list) into /opt/python on a Debian 13 slim
# base: the dev venv's kind of build (static libpython, clang PGO + ThinLTO + BOLT); the official python image's
# CPython (shared libpython, GCC) cost +11-13% CPU per turn. Its OpenSSL, SQLite, zlib, expat, libffi, xz, bzip2,
# ncurses and libedit are statically linked: trivy's Debian package scan does not see them, so a PYTHON_VERSION or
# UV_IMAGE bump is a dependency update (reports/perf/sizing.md lists the bundled versions).

ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168
ARG PYTHON_VERSION=3.12.15

FROM ${UV_IMAGE} AS uv

FROM ${DEBIAN_IMAGE} AS build
ARG PYTHON_VERSION
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/noema/venv
# The interpreter without what no Noema process runs: Tcl/Tk and the GUI modules, pip (the venv has none; uv
# installs), the headers and the shared libpython (the interpreter links libpython statically; no wheel needs it).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install --no-bin "${PYTHON_VERSION}" \
 && cd /opt/python/cpython-"${PYTHON_VERSION}"-linux-x86_64-gnu \
 && rm -rf include lib/libtcl* lib/libtk* lib/tcl* lib/tk* lib/itcl* lib/thread* lib/libpython3* \
      lib/python3.12/tkinter lib/python3.12/idlelib lib/python3.12/turtledemo lib/python3.12/lib-dynload/_tkinter.* \
      lib/python3.12/site-packages/pip lib/python3.12/site-packages/pip-* bin/pip* bin/idle* bin/2to3* bin/pydoc* \
 && rm -rf /opt/python/.lock /opt/python/.temp /opt/python/.gitignore \
 && ./bin/python3.12 -I -c "import ssl, sqlite3, zlib, ctypes, lzma, bz2, uuid, zoneinfo"
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --python "${PYTHON_VERSION}"
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --python "${PYTHON_VERSION}"

FROM ${DEBIAN_IMAGE} AS noema-api
ARG PYTHON_VERSION
# Debian 13 (trixie) slim (defect 29, SE13): the point updates published after the pinned digest are applied at build
# time; what trivy still reports without a fixed version is waived in deploy/docker/.trivyignore.yaml. ca-certificates,
# netbase and tzdata as in the official python image (the bundled OpenSSL reads /etc/ssl/certs).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates netbase tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10002 noema \
 && useradd --system --uid 10001 --gid 10002 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin noema
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/noema/venv /opt/noema/venv
# PYTHON_VERSION as in the official python image (read by tests/load/run.py's provenance)
ENV PATH=/opt/noema/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHON_VERSION=${PYTHON_VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN python -I -c "import noema, fastapi, uvicorn, uvloop, httptools"
LABEL org.opencontainers.image.title="noema-api" \
      org.opencontainers.image.description="Noema api process (also the base of the jobs and media-worker image)"
USER 10001:10002
WORKDIR /opt/noema
EXPOSE 8000 9464
STOPSIGNAL SIGTERM
# spec-11 §7: SIGTERM -> /readyz 503 and the drain hooks at once; uvicorn stops accepting NOEMA_DRAIN_DELAY_S later and
# waits up to 35 s (above the 30 s turn grace) for the open streams before the lifespan's stop() flushes the outbox.
CMD ["uvicorn", "--factory", "noema.api.app:create_app", "--loop", "uvloop", "--http", "httptools", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "35"]
