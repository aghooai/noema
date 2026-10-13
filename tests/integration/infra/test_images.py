"""The built images (make images): contents, users, offline settings and the media-sandbox privilege ladder.

Each test skips when its image is missing. The ladder runs deploy/sandbox/selftest.py under the compose privileges
and under each relaxation recorded in deploy/sandbox/PRIVILEGES.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.infra.helpers import SANDBOX_DIR, require_image, run

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("docker")]

API_IMAGE = "noema-api:dev"
WORKER_IMAGE = "noema-worker:dev"
MEDIA_IMAGE = "noema-media:dev"
# spec-01 §2 rule 7 (the media group) plus dev-only tools: none may be importable in the api/worker images.
FORBIDDEN_IN_API = (
    "PIL",
    "pillow_heif",
    "pikepdf",
    "docling",
    "docling_core",
    "docling_parse",
    "torch",
    "onnxruntime",
    "pypdfium2",
    "pytesseract",
    "magika",
    "magic",
    "markdown_it",
    "pytest",
    "moto",
    "mypy",
)
DISTRIBUTIONS = (
    "import importlib.metadata as m; print('\\n'.join(sorted(f'{d.name}=={d.version}' for d in m.distributions())))"
)


def docker_python(image: str, code: str, *extra: str) -> str:
    out = run(["docker", "run", "--rm", "--network", "none", "--read-only", *extra, image, "python", "-I", "-c", code])
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def inspect(image: str) -> dict[str, Any]:
    out = run(["docker", "image", "inspect", image])
    assert out.returncode == 0, out.stderr
    doc: list[dict[str, Any]] = json.loads(out.stdout)
    return doc[0]


def test_api_image_has_no_media_parsers_or_dev_tools() -> None:
    require_image(API_IMAGE)
    code = f"import importlib.util as u; print([m for m in {FORBIDDEN_IN_API!r} if u.find_spec(m) is not None])"
    assert docker_python(API_IMAGE, code) == "[]"
    assert docker_python(
        API_IMAGE, "import noema, fastapi, uvloop, httptools, asyncpg, redis, aiobotocore; print('ok')"
    )


def test_api_and_worker_images_run_as_the_noema_uid() -> None:
    for image in (require_image(API_IMAGE), require_image(WORKER_IMAGE)):
        assert inspect(image)["Config"]["User"] == "10001:10002"
        assert docker_python(image, "import os; print(os.getuid(), os.getgid())") == "10001 10002"


def test_worker_image_has_exactly_the_api_venv() -> None:
    api, worker = require_image(API_IMAGE), require_image(WORKER_IMAGE)
    assert docker_python(worker, DISTRIBUTIONS) == docker_python(api, DISTRIBUTIONS)
    cmd = inspect(worker)["Config"]["Cmd"]
    assert cmd[:3] == ["python", "-m", "noema.entrypoints.jobs"]


def test_api_image_command_is_the_uvicorn_factory() -> None:
    cmd = inspect(require_image(API_IMAGE))["Config"]["Cmd"]
    assert cmd[:3] == ["uvicorn", "--factory", "noema.api.app:create_app"]
    assert cmd[cmd.index("--loop") + 1] == "uvloop"
    assert float(cmd[cmd.index("--timeout-graceful-shutdown") + 1]) > 30  # above the drain grace (spec-11 §7)


def test_the_images_build_the_real_composition_without_tests() -> None:
    code = (
        "import sys; from noema.composition import build; s = build('dev', environ={}); "
        "print(set(s.wiring.values()), sorted({m.split('.')[0] for m in sys.modules} & {'tests', 'moto', 'pytest'}))"
    )
    for image in (require_image(API_IMAGE), require_image(WORKER_IMAGE)):
        assert docker_python(image, code, "--tmpfs", "/tmp") == "{'real'} []"


def test_media_image_is_offline_and_credential_free() -> None:
    env = dict(e.split("=", 1) for e in inspect(require_image(MEDIA_IMAGE))["Config"]["Env"])
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["DOCLING_ARTIFACTS_PATH"] == "/opt/docling-models"
    assert env["TESSDATA_PREFIX"] == "/usr/share/tesseract-ocr/5/tessdata"
    assert not [k for k in env if k.startswith(("AWS_", "NOEMA_DB_")) or "SECRET" in k]


def test_media_image_carries_the_sandbox_files_and_the_noema_wheel() -> None:
    image = require_image(MEDIA_IMAGE)
    code = (
        "import pathlib, noema, sys; "
        "print(noema.__file__.startswith('/opt/noema-sandbox/venv/lib/python3.12/site-packages/'), "
        "pathlib.Path('/etc/noema-sandbox/seccomp.kafel').read_text() == sys.stdin.read())"
    )
    out = run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", image, "python", "-I", "-c", code],
        input_text=(SANDBOX_DIR / "seccomp.kafel").read_text(),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "True"]
