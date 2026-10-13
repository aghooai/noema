"""SEC-SBX-04 (ADR-002 §6, spec-09 §6.6): a compromised extractor's manifest and outputs are rejected by the trusted
validator, and nothing unlisted survives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from noema.media.sandbox.manifest import ManifestInvalid, validate

from .media_fixtures import png

pytestmark = pytest.mark.unit
PNG = png(8, 8)


def entry(
    name: str, data: bytes, *, kind: str = "raster", media_type: str = "image/png", **extra: Any
) -> dict[str, Any]:
    return {
        "path": f"out/{name}",
        "kind": kind,
        "media_type": media_type,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **extra,
    }


def write(out: Path, outputs: list[dict[str, Any]], **overrides: Any) -> None:
    doc = {
        "v": 1,
        "kind": "image.normalize",
        "extractor": "Pillow@test",
        "outputs": outputs,
        "facts": {"width": 8, "height": 8, "frames": 1},
        "rejection": None,
        "warnings": [],
        **overrides,
    }
    (out / "manifest.json").write_text(json.dumps(doc))


def good(out: Path) -> list[dict[str, Any]]:
    outputs = []
    for name, kind in (("raster.png", "raster"), ("raster_1568.png", "raster_1568"), ("thumbnail.png", "thumbnail")):
        (out / name).write_bytes(PNG)
        outputs.append(entry(name, PNG, kind=kind, width=8, height=8))
    return outputs


def check(out: Path, *, max_outputs: int = 3, cap: int = 1 << 20, kind: str = "image.normalize") -> Any:
    return validate(kind, out, max_outputs=max_outputs, output_cap_bytes=cap, wall_ms=5)  # type: ignore[arg-type]


def test_a_valid_manifest_gives_ok_with_trusted_outputs(tmp_path: Path) -> None:
    write(tmp_path, good(tmp_path))
    (tmp_path / "stray.png").write_bytes(PNG)
    result = check(tmp_path)
    assert result.status == "ok" and result.extractor == "Pillow@test"
    assert [o.kind for o in result.outputs] == ["raster", "raster_1568", "thumbnail"]
    assert result.facts == {"width": 8, "height": 8, "frames": 1}
    assert not (tmp_path / "stray.png").exists()  # unlisted files are deleted, never uploaded


def test_a_rejection_lists_no_outputs(tmp_path: Path) -> None:
    write(tmp_path, [], rejection={"code": "pixel_limit"}, facts={})
    result = check(tmp_path)
    assert result.status == "rejected" and result.rejection_code == "pixel_limit" and result.outputs == ()
    write(tmp_path, good(tmp_path), rejection={"code": "pixel_limit"})
    with pytest.raises(ManifestInvalid):
        check(tmp_path)


@pytest.mark.parametrize(
    "attack",
    [
        "dotdot_path",
        "absolute_path",
        "size_lie",
        "sha_lie",
        "html_as_png",
        "too_many",
        "unknown_key",
        "wrong_kind",
        "bad_rejection",
        "bad_warning",
        "bad_facts",
        "duplicate_key",
        "nan",
        "jpeg_named_png",
        "deep_nesting",
    ],
)
def test_manifest_attacks_are_rejected(tmp_path: Path, attack: str) -> None:
    outputs = good(tmp_path)
    overrides: dict[str, Any] = {}
    if attack == "dotdot_path":
        outputs[0]["path"] = "out/../../etc/passwd.png"
    elif attack == "absolute_path":
        outputs[0]["path"] = "/etc/raster.png"
    elif attack == "size_lie":
        outputs[0]["bytes"] = len(PNG) + 1
    elif attack == "sha_lie":
        outputs[0]["sha256"] = "0" * 64
    elif attack == "html_as_png":
        (tmp_path / "raster.png").write_bytes(b"<html><script>x</script></html>")
        outputs[0] = entry("raster.png", (tmp_path / "raster.png").read_bytes(), width=8, height=8)
    elif attack == "too_many":
        outputs.append(entry("extra.png", PNG, kind="thumbnail"))
        (tmp_path / "extra.png").write_bytes(PNG)
    elif attack == "unknown_key":
        outputs[0]["exec"] = "rm -rf /"
    elif attack == "wrong_kind":
        outputs[0]["kind"] = "pdf_sanitized"
    elif attack == "bad_rejection":
        overrides["rejection"] = {"code": "malware_detected"}
    elif attack == "bad_warning":
        overrides["warnings"] = ["<script>"]
    elif attack == "bad_facts":
        overrides["facts"] = {"width": 8, "height": 8, "frames": 1, "extra": "x"}
    elif attack == "jpeg_named_png":
        outputs[1]["media_type"] = "image/jpeg"
    write(tmp_path, outputs, **overrides)
    if attack == "duplicate_key":
        text = (tmp_path / "manifest.json").read_text()
        (tmp_path / "manifest.json").write_text(text[:-1] + ', "v": 1}')
    if attack == "nan":
        text = (tmp_path / "manifest.json").read_text()
        (tmp_path / "manifest.json").write_text(text.replace('"width": 8', '"width": NaN', 1))
    if attack == "deep_nesting":  # under 1 MiB, but deeper than the parser's stack (RecursionError, C1 review)
        text = (tmp_path / "manifest.json").read_text()
        (tmp_path / "manifest.json").write_text(text.replace('"warnings": []', '"warnings": ' + "[" * 200_000, 1))
    with pytest.raises(ManifestInvalid):
        check(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"manifest.json"}  # nothing survives a failed validation


def test_ten_thousand_and_one_outputs_and_a_2_mib_manifest(tmp_path: Path) -> None:
    pages = [entry(f"page-{i:05d}.png", PNG, kind="page_images", page=i) for i in range(1, 10_002)]
    doc = {"v": 1, "kind": "pdf.render", "extractor": "x", "outputs": pages, "rejection": None, "warnings": []}
    doc["facts"] = {"pages": 600, "text_layer_pages": []}
    (tmp_path / "manifest.json").write_text(json.dumps(doc))
    with pytest.raises(ManifestInvalid):
        check(tmp_path, kind="pdf.render", max_outputs=600)
    (tmp_path / "manifest.json").write_bytes(b" " * (2 * 1024 * 1024) + b"{}")
    with pytest.raises(ManifestInvalid, match="1 MiB"):
        check(tmp_path)


def test_symlinked_or_hard_linked_outputs_are_rejected(tmp_path: Path) -> None:
    target = tmp_path.parent / f"{tmp_path.name}-target.png"
    target.write_bytes(PNG)
    outputs = good(tmp_path)
    (tmp_path / "raster.png").unlink()
    (tmp_path / "raster.png").symlink_to(target)
    write(tmp_path, outputs)
    with pytest.raises(ManifestInvalid):
        check(tmp_path)
    outputs = good(tmp_path)
    (tmp_path / "raster.png").unlink()
    os.link(target, tmp_path / "raster.png")
    write(tmp_path, outputs)
    with pytest.raises(ManifestInvalid):
        check(tmp_path)
    assert target.read_bytes() == PNG  # the link target is untouched


def test_a_symlinked_manifest_and_bad_text_are_rejected(tmp_path: Path) -> None:
    real = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    real.write_text("{}")
    (tmp_path / "manifest.json").symlink_to(real)
    with pytest.raises(ManifestInvalid):
        check(tmp_path)
    (tmp_path / "manifest.json").unlink()
    for bad in (b"bad \xff utf-8", b"nul \x00 byte"):
        (tmp_path / "ocr_text.txt").write_bytes(bad)
        doc_out = [entry("ocr_text.txt", bad, kind="ocr_text", media_type="text/plain")]
        write(tmp_path, doc_out, kind="ocr", facts={})
        with pytest.raises(ManifestInvalid):
            check(tmp_path, kind="ocr", max_outputs=1)


def test_output_over_the_cap_is_rejected(tmp_path: Path) -> None:
    write(tmp_path, good(tmp_path))
    with pytest.raises(ManifestInvalid):
        check(tmp_path, cap=len(PNG) - 1)
