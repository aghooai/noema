# noema-media: the media-sandbox image (ADR-002 §8, IMPL-081). Debian 12 (bookworm, the latest point release; defect 29:
# the runtime stage applies the point updates at build time, deploy/docker/.trivyignore.yaml waives what has no fixed
# version). It stays on bookworm while noema-api moves to trixie: trixie's libmagic 5.46 finds `%PDF-` anywhere in the
# first 1024 bytes (5.44: about 256), so detect would label a Markdown note quoting the header `application/pdf` (the
# media pipeline's documented verdict, tests/unit/media_pipeline/test_detect.py) until media-pipeline adapts. With
#   - nsjail built from a pinned upstream tag (bundled kafel), the jailer of every extraction job;
#   - tesseract-ocr 5 + eng, libmagic1 + libmagic-mgc, qpdf (CLI for tests and the selftest only);
#   - the venv /opt/noema-sandbox/venv = the locked runtime dependencies + the `media` dependency group + the noema wheel;
#   - the Docling models baked into /opt/docling-models (the sandbox has no network at run time);
#   - deploy/sandbox/* (kafel policy, selftest) under /etc/noema-sandbox.
# Targets: `media-sandbox` (runtime; the last stage, so the default) and `media-test` (adds the dev + fuzz groups and
# the tests; make test-media). Build from the repo root:
#   docker build -f deploy/docker/media-sandbox.Dockerfile --target media-sandbox -t noema-media:dev .
# The context is filtered by media-sandbox.Dockerfile.dockerignore (an allowlist).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.10@sha256:29bd45092ea8902c0bbb7f0a338f0494a382b1f4b18355df5be270ade679ff1d

FROM ${UV_IMAGE} AS uv

# --- nsjail ------------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS nsjail-build
ARG NSJAIL_TAG=3.6
ARG NSJAIL_COMMIT=f78475530b46d0186111a9096b30725f816b55fe
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git g++ make pkg-config \
      protobuf-compiler libprotobuf-dev libnl-route-3-dev bison flex \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${NSJAIL_TAG}" --recurse-submodules --shallow-submodules \
      https://github.com/google/nsjail /src/nsjail \
 && test "$(git -C /src/nsjail rev-parse HEAD)" = "${NSJAIL_COMMIT}" \
 && make -C /src/nsjail -j"$(nproc)" \
 && install -m 0755 /src/nsjail/nsjail /usr/local/bin/nsjail

# --- Python dependencies (no project): cached until pyproject.toml or uv.lock change ---------------------------------
FROM ${PYTHON_IMAGE} AS venv
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/noema-sandbox/venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group media --no-install-project --python /usr/local/bin/python3

# --- Docling models, downloaded at build time (layout + TableFormer: what pdf.markdown uses with do_ocr=False) -------
# docling 2.137 fetches the layout model at revision `main` (TableFormer is pinned), so MANIFEST.sha256 records
# exactly what was baked; the HF commit of each repo is under */.cache/huggingface/download/*.metadata.
FROM venv AS models
ARG DOCLING_MODELS="layout tableformer"
RUN /opt/noema-sandbox/venv/bin/docling-tools models download ${DOCLING_MODELS} -o /opt/docling-models \
 && (cd /opt/docling-models && find . -type f -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256) \
 && chmod -R a+rX,go-w /opt/docling-models

# --- the noema wheel (installed non-editable, so jails that mount only /opt/noema-sandbox can import it) --------------
FROM venv AS wheel
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /dist

# --- media-base: everything but noema (sandboxd as container root; jails as uid 100000+slot; network_mode: none) ----
# Layers that change with src/ come last (the wheel in media-sandbox, the sources in media-test).
FROM ${PYTHON_IMAGE} AS media-base
ARG NSJAIL_TAG=3.6
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng libmagic1 libmagic-mgc qpdf libprotobuf32 libnl-route-3-200 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=nsjail-build /usr/local/bin/nsjail /usr/local/bin/nsjail
COPY --from=models /opt/docling-models /opt/docling-models
COPY --from=venv /opt/noema-sandbox/venv /opt/noema-sandbox/venv
COPY deploy/sandbox/ /etc/noema-sandbox/
RUN chmod -R a+rX,go-w /etc/noema-sandbox \
 && nsjail --help >/dev/null 2>&1 \
 && test -d /usr/share/tesseract-ocr/5/tessdata
# Process-level offline settings; jails get theirs from the nsjail config (keep_env: false, ADR-002 §3).
ENV PATH=/opt/noema-sandbox/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    NOEMA_SANDBOX_POLICY=/etc/noema-sandbox/seccomp.kafel
LABEL org.opencontainers.image.title="noema-media" \
      org.opencontainers.image.description="Noema media sandbox: nsjail ${NSJAIL_TAG}, tesseract 5, libmagic, qpdf, media group, Docling models"
WORKDIR /
CMD ["python", "-I", "-m", "noema.media.sandbox.sandboxd"]

# --- media-test: + the dev and fuzz groups, the current noema wheel and the tests (make test-media) ------------------
FROM media-base AS media-test
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/noema-sandbox/venv
WORKDIR /opt/noema-test
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group media --group fuzz --no-install-project --inexact --python /usr/local/bin/python3
RUN --mount=type=bind,from=wheel,source=/dist,target=/dist \
    uv pip install --no-deps --python /opt/noema-sandbox/venv/bin/python /dist/*.whl \
 && python -I -c "import noema"
COPY conftest.py ./
COPY tests ./tests
# The root filesystem is read-only under compose: no bytecode, pytest cache or hypothesis database next to the tests.
ENV PYTHONDONTWRITEBYTECODE=1 \
    HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis \
    NOEMA_MEDIA_IMAGE=1
CMD ["pytest", "-p", "no:cacheprovider", "tests/integration/infra/media_image"]

# --- media-sandbox: the runtime image (the last stage, so the default target) ----------------------------------------
FROM media-base AS media-sandbox
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,from=wheel,source=/dist,target=/dist \
    UV_COMPILE_BYTECODE=1 uv pip install --no-deps --python /opt/noema-sandbox/venv/bin/python /dist/*.whl \
 && python -I -c "import noema"
