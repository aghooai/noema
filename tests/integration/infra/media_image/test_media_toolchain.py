"""Media image smoke tests (ADR-002 §8): run INSIDE noema-media-test by `make test-media` (network_mode: none).

On the host they skip: the host has no nsjail, tesseract or qpdf (spec-07 §7). Fixtures are the probe files of
spec-07 MED-02, mounted read-only at their host path by `make test-media`.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NOEMA_MEDIA_IMAGE") != "1", reason="runs inside the media image: make test-media"
    ),
]

FIXTURES = Path("/codes/aghooai/probes/fixtures")
NUMBER_PNG = FIXTURES / "number.png"
INVOICE_PDF = FIXTURES / "invoice.pdf"
NSJAIL_SMOKE_CFG = """
mode: ONCE
hostname: "sbx"
cwd: "/tmp"
time_limit: 30
keep_env: false
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
clone_newcgroup: true
iface_no_lo: true
uidmap { inside_id: "65534" outside_id: "100000" count: 1 }
gidmap { inside_id: "65534" outside_id: "100000" count: 1 }
mount_proc: false
mount { src: "/usr" dst: "/usr" is_bind: true rw: false }
mount { src: "/lib" dst: "/lib" is_bind: true rw: false }
mount { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
mount { src: "/etc/ld.so.cache" dst: "/etc/ld.so.cache" is_bind: true rw: false }
mount { src: "/opt/noema-sandbox" dst: "/opt/noema-sandbox" is_bind: true rw: false }
mount { dst: "/tmp" fstype: "tmpfs" rw: true options: "size=16m" nosuid: true nodev: true }
"""


@pytest.fixture(scope="module")
def fixtures() -> Path:
    if not (NUMBER_PNG.is_file() and INVOICE_PDF.is_file()):
        pytest.skip(f"probe fixtures not mounted at {FIXTURES}")
    return FIXTURES


def test_container_has_no_network_interface_but_lo() -> None:
    assert sorted(os.listdir("/sys/class/net")) == ["lo"]


def test_offline_settings_are_baked_in() -> None:
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    models = Path(os.environ["DOCLING_ARTIFACTS_PATH"])
    assert models == Path("/opt/docling-models")
    assert sorted(p.name for p in models.iterdir()), "Docling models missing"
    assert Path(os.environ["TESSDATA_PREFIX"], "eng.traineddata").is_file()


def test_tesseract_reads_number_png(fixtures: Path) -> None:
    out = subprocess.run(
        ["tesseract", str(NUMBER_PNG), "stdout", "-l", "eng", "--psm", "3"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "4471" in out.stdout
    version = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=True).stdout
    assert version.startswith("tesseract 5.")


def test_pikepdf_opens_invoice_and_qpdf_checks_it(fixtures: Path) -> None:
    import pikepdf

    with pikepdf.open(INVOICE_PDF) as pdf:
        assert len(pdf.pages) == 1
    subprocess.run(["qpdf", "--check", str(INVOICE_PDF)], capture_output=True, check=True, timeout=60)


def test_pypdfium2_renders_invoice(fixtures: Path) -> None:
    import pypdfium2  # type: ignore[import-untyped]

    doc = pypdfium2.PdfDocument(INVOICE_PDF)
    try:
        page = doc[0]
        image = page.render(scale=1568 / max(page.get_size())).to_pil()
        assert max(image.size) == pytest.approx(1568, abs=2)
    finally:
        doc.close()


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_docling_converts_invoice_to_markdown_offline(fixtures: Path) -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(INVOICE_PDF)
    markdown = result.document.export_to_markdown()
    assert "Invoice number 4471" in markdown
    assert len(result.document.pages) == 1


def test_pillow_heif_opens_a_generated_heic(fixtures: Path) -> None:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    source = Image.open(NUMBER_PNG).convert("RGB")
    buf = io.BytesIO()
    source.save(buf, format="HEIF", quality=90)
    data = buf.getvalue()
    assert data[4:12] == b"ftypheic"
    with Image.open(io.BytesIO(data), formats=["HEIF"]) as heic:
        heic.load()
        assert heic.size == source.size


def test_libmagic_and_magika_identify_the_fixtures(fixtures: Path) -> None:
    import magic
    from magika import Magika

    assert magic.from_file(str(INVOICE_PDF), mime=True) == "application/pdf"
    assert magic.from_file(str(NUMBER_PNG), mime=True) == "image/png"
    detector = Magika()
    assert detector.identify_path(INVOICE_PDF).output.label == "pdf"
    assert detector.identify_path(NUMBER_PNG).output.label == "png"


def test_nsjail_runs_a_command_in_a_network_namespace(tmp_path: Path) -> None:
    cfg = tmp_path / "smoke.cfg"
    cfg.write_text(NSJAIL_SMOKE_CFG)
    probe = "import os, socket; print(os.getuid(), [n for _, n in socket.if_nameindex()])"
    nsjail = shutil.which("nsjail")
    assert nsjail is not None
    out = subprocess.run(
        [nsjail, "--config", str(cfg), "--quiet", "--", "/opt/noema-sandbox/venv/bin/python", "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "65534 ['lo']"


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_sandbox_selftest_passes_under_the_compose_privileges() -> None:
    """SEC-SBX-01: ADR-002 §2 checks 1-10 with the kafel policy shipped in the image."""
    out = subprocess.run(
        ["/etc/noema-sandbox/selftest.sh", "--json"], capture_output=True, text=True, timeout=240, check=False
    )
    assert out.stdout.startswith("{"), out.stderr[-2000:]
    report = json.loads(out.stdout)
    failed = [r for r in report["results"] if not r["passed"]]
    assert report["status"] == 0, failed or out.stderr
    assert {r["adr"] for r in report["results"]} == set(range(1, 11))
