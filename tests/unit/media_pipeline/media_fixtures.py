"""Generated media fixtures (spec-07 §7 MED-01…04, ADR-002 SEC-SBX-03): small, deterministic, built in memory.

Probe fixtures (`/codes/aghooai/probes/fixtures`: number.png reads 4471, invoice.pdf) are used where present.
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path
from typing import Any, Final

PROBES: Final = Path("/codes/aghooai/probes/fixtures")
NUMBER_PNG: Final = PROBES / "number.png"
INVOICE_PDF: Final = PROBES / "invoice.pdf"


def png(width: int = 64, height: int = 48, *, mode: str = "RGB", color: object = (200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")  # type: ignore[arg-type]
    return buf.getvalue()


def noisy_png(size: int = 1568) -> bytes:
    """A noisy PNG whose 1568 px rendition exceeds 3,750,000 bytes (→ raster_1568.jpg)."""
    import random

    from PIL import Image

    rnd = random.Random(7)
    img = Image.frombytes("RGB", (size, size), rnd.randbytes(size * size * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def jpeg_with_exif(width: int = 80, height: int = 40, orientation: int = 6) -> bytes:
    """A landscape JPEG whose EXIF orientation 6 means "rotate 90° clockwise" (upright = portrait)."""
    from PIL import Image

    img = Image.new("RGB", (width, height), (10, 120, 200))
    img.paste((255, 255, 0), (0, 0, width // 4, height))  # a marker band on the left edge
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "CanaryCam"  # Make: must not survive
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes(), icc_profile=b"\x00" * 128, quality=90)
    return buf.getvalue()


def animated(fmt: str = "GIF", frames: int = 3) -> bytes:
    from PIL import Image

    images = [Image.new("RGB", (32, 32), (i * 60, 0, 0)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format=fmt, save_all=True, append_images=images[1:], duration=50, loop=0)
    return buf.getvalue()


def heic(source: bytes | None = None) -> bytes:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    img = Image.open(io.BytesIO(source or png(96, 64))).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="HEIF", quality=80)
    return buf.getvalue()


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_header_only(width: int, height: int) -> bytes:
    """A PNG whose IHDR claims `width`×`height` with a tiny IDAT (the pixel guard fires on the header)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 16)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def png_ztxt_bomb(megabytes: int = 64) -> bytes:
    """A valid 1×1 PNG with a zTXt chunk that inflates to `megabytes` MiB (Pillow: Decompressed Data Too Large)."""
    base = png(1, 1)
    bomb = zlib.compress(b"A" * (megabytes * 1024 * 1024), 9)
    end = base.rindex(b"IEND") - 4
    return base[:end] + _chunk(b"zTXt", b"Comment\x00\x00" + bomb) + base[end:]


def truncated(data: bytes, keep: float = 0.5) -> bytes:
    return data[: max(16, int(len(data) * keep))]


def jpeg(width: int = 64, height: int = 64) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (50, 50, 50)).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# --- PDFs (spec-09 §6.3 fixtures) -------------------------------------------------------------------------------------
def _pdf_bytes(build: object, *, pages: int = 1, **save: object) -> bytes:
    import pikepdf

    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(200, 200))
    if callable(build):
        build(pdf)
    buf = io.BytesIO()
    pdf.save(buf, **save)  # type: ignore[arg-type]
    return buf.getvalue()


def text_pdf(text: str = "Invoice number 4471", pages: int = 1) -> bytes:
    """A PDF with a real text layer on every page."""
    import pikepdf

    def build(pdf: pikepdf.Pdf) -> None:
        font = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica)
        )
        for page in pdf.pages:
            page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            page.obj.Contents = pdf.make_stream(f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode())

    return _pdf_bytes(build, pages=pages)


def _action(pdf: object, kind: str, **extra: Any) -> object:
    import pikepdf

    return pikepdf.Dictionary({"/S": pikepdf.Name("/" + kind), **{f"/{k}": v for k, v in extra.items()}})


def _annot(pdf: object, subtype: str, **extra: Any) -> object:
    import pikepdf

    base = {"/Type": pikepdf.Name.Annot, "/Subtype": pikepdf.Name("/" + subtype), "/Rect": [0, 0, 10, 10]}
    return pikepdf.Dictionary({**base, **{f"/{k}": v for k, v in extra.items()}})


def _add_annots(pdf: object, *annots: object) -> None:
    import pikepdf

    page = pdf.pages[0]  # type: ignore[attr-defined]
    page.obj.Annots = pikepdf.Array([pdf.make_indirect(a) for a in annots])  # type: ignore[attr-defined]


def malicious_pdfs() -> dict[str, tuple[bytes, set[str]]]:
    """One PDF per spec-09 §6.3 row (and the extra fixtures): name -> (bytes, expected active_content facts)."""
    import pikepdf

    js = {"JS": pikepdf.String("app.alert(1)")}
    out: dict[str, tuple[bytes, set[str]]] = {}

    def case(name: str, facts: set[str], build: object) -> None:
        out[name] = (_pdf_bytes(build), facts)

    case("open_action", {"open_action"}, lambda p: setattr(p.Root, "OpenAction", _action(p, "JavaScript", **js)))
    case(
        "catalog_aa", {"additional_actions"}, lambda p: setattr(p.Root, "AA", pikepdf.Dictionary(WC=_action(p, "GoTo")))
    )
    case(
        "page_aa",
        {"additional_actions"},
        lambda p: setattr(p.pages[0].obj, "AA", pikepdf.Dictionary(O=_action(p, "GoTo"))),
    )
    case(
        "names_js",
        {"javascript"},
        lambda p: setattr(
            p.Root,
            "Names",
            pikepdf.Dictionary(
                JavaScript=pikepdf.Dictionary(
                    Names=pikepdf.Array([pikepdf.String("x"), _action(p, "JavaScript", **js)])
                )
            ),
        ),
    )
    case("link_js", {"javascript"}, lambda p: _add_annots(p, _annot(p, "Link", A=_action(p, "JavaScript", **js))))
    case(
        "launch",
        {"launch"},
        lambda p: _add_annots(p, _annot(p, "Link", A=_action(p, "Launch", F=pikepdf.String("c.exe")))),
    )
    for remote in ("GoToR", "GoToE", "SubmitForm", "ImportData", "ResetForm"):
        case(
            f"remote_{remote}",
            {"remote_action"},
            lambda p, r=remote: _add_annots(p, _annot(p, "Link", A=_action(p, r))),
        )
    case(
        "uri_js",
        {"remote_action"},
        lambda p: _add_annots(p, _annot(p, "Link", A=_action(p, "URI", URI=pikepdf.String("javascript:alert(1)")))),
    )
    for media in ("Rendition", "Movie", "Sound", "RichMediaExecute"):
        case(f"action_{media}", {"rich_media"}, lambda p, m=media: _add_annots(p, _annot(p, "Link", A=_action(p, m))))
    for sub in ("RichMedia", "Screen", "Movie", "Sound", "3D"):
        case(f"annot_{sub}", {"rich_media"}, lambda p, s=sub: _add_annots(p, _annot(p, s)))
    case("file_attachment", {"embedded_file"}, lambda p: _add_annots(p, _annot(p, "FileAttachment")))
    case(
        "embedded_files",
        {"embedded_file"},
        lambda p: setattr(p.Root, "Names", pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array()))),
    )
    case(
        "ef_key",
        {"embedded_file"},
        lambda p: setattr(
            p.Root,
            "Extra",
            pikepdf.Dictionary(Type=pikepdf.Name.Filespec, EF=pikepdf.Dictionary(F=p.make_stream(b"payload"))),
        ),
    )
    case("collection", {"embedded_file"}, lambda p: setattr(p.Root, "Collection", pikepdf.Dictionary()))
    case(
        "xfa",
        {"xfa"},
        lambda p: setattr(p.Root, "AcroForm", pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=p.make_stream(b"<xdp/>"))),
    )
    case("needs_rendering", {"xfa"}, lambda p: setattr(p.Root, "NeedsRendering", True))
    case(
        "chain_goto_next_js",
        {"javascript"},
        lambda p: _add_annots(
            p,
            _annot(
                p,
                "Link",
                A=_action(
                    p, "GoTo", D=pikepdf.Array([p.pages[0].obj, pikepdf.Name.Fit]), Next=_action(p, "JavaScript", **js)
                ),
            ),
        ),
    )
    case(
        "field_aa",
        {"additional_actions"},
        lambda p: setattr(
            p.Root,
            "AcroForm",
            pikepdf.Dictionary(
                Fields=pikepdf.Array(
                    [
                        p.make_indirect(
                            pikepdf.Dictionary(
                                FT=pikepdf.Name.Tx,
                                T=pikepdf.String("f"),
                                AA=pikepdf.Dictionary(K=_action(p, "JavaScript", **js)),
                            )
                        )
                    ]
                )
            ),
        ),
    )
    case(
        "safe_links",
        set(),
        lambda p: _add_annots(
            p,
            _annot(p, "Link", A=_action(p, "URI", URI=pikepdf.String("https://x.example"))),
            _annot(p, "Link", A=_action(p, "Named", N=pikepdf.Name.NextPage)),
        ),
    )
    out["incremental_open_action"] = (incremental_open_action(), {"open_action"})
    return out


def incremental_open_action() -> bytes:
    """A clean PDF plus a hand-written incremental update (new catalog revision, xref section, /Prev trailer) that
    adds /OpenAction: the sanitizer must see it and the rewrite must drop the earlier revision."""
    import re

    import pikepdf

    with pikepdf.open(io.BytesIO(text_pdf())) as pdf:
        root_num = pdf.Root.objgen[0]
        pages_num = pdf.Root.Pages.objgen[0]
        size = int(pdf.trailer.Size)
        buf = io.BytesIO()
        pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=False)
    base = buf.getvalue()
    prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
    offset = len(base)
    obj = (
        f"{root_num} 0 obj\n<< /Type /Catalog /Pages {pages_num} 0 R "
        "/OpenAction << /S /JavaScript /JS (app.alert(2)) >> >>\nendobj\n"
    ).encode()
    xref_at = offset + len(obj)
    tail = (
        f"xref\n{root_num} 1\n{offset:010d} 00000 n \ntrailer\n<< /Size {size} /Root {root_num} 0 R /Prev {prev} >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return base + obj + tail


def encrypted_pdf() -> bytes:
    """Encrypted with an empty user password (opens without a password, still `encrypted_pdf`)."""
    import pikepdf

    return _pdf_bytes(None, encryption=pikepdf.Encryption(owner="owner-secret", user="", R=6))


def many_pages_pdf(pages: int = 601) -> bytes:
    return _pdf_bytes(None, pages=pages)


def pdf_zip_polyglot() -> bytes:
    """A PDF with a ZIP archive appended (mitra-style PDF+ZIP)."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.txt", "hello")
    return text_pdf() + buf.getvalue()


def png_pdf_polyglot() -> bytes:
    """PNG bytes followed by a PDF (PNG+PDF)."""
    return png(16, 16) + text_pdf()


def zip_bomb_as_pdf() -> bytes:
    """A deflate bomb in a ZIP, named .pdf by the uploader."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\x00" * (32 * 1024 * 1024))
    return buf.getvalue()


def png_of_size(total: int, *, side: int = 1000) -> bytes:
    """A valid noisy PNG padded to exactly `total` bytes with a private ancillary chunk before IEND."""
    import random

    base = noisy_png(side)
    need = total - len(base) - 12
    if need < 0:
        raise ValueError("total is smaller than the base image")
    pad = random.Random(3).randbytes(need)
    end = base.rindex(b"IEND") - 4
    return base[:end] + _chunk(b"prVt", pad) + base[end:]


def gif_canvas_bomb(width: int = 40_000, height: int = 40_000) -> bytes:
    """A GIF whose logical screen and one 1x1 frame claim a 40k x 40k canvas (a frame/pixel bomb by header)."""
    header = b"GIF89a" + struct.pack("<HHBBB", width, height, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    frame = b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0) + b"\x02\x02\x44\x01\x00"
    return header + frame + b"\x3b"


def webp_canvas_bomb(width: int = 16_384, height: int = 16_384) -> bytes:
    """An extended WebP (VP8X) whose canvas claims 16k x 16k with no real image data."""
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
    vp8x += (height - 1).to_bytes(3, "little")
    body = b"WEBP" + vp8x
    return b"RIFF" + struct.pack("<I", len(body)) + body


def shared_noise_pdf(pages: int = 3) -> bytes:
    """A small PDF whose every page shows one shared noisy JPEG: each 1568 px page render is a ~5 MiB PNG, so the
    render output grows with the page count while the upload stays small (the spool-exhaustion case)."""
    import random

    import pikepdf
    from PIL import Image

    rnd = random.Random(11)
    noise = Image.frombytes("RGB", (1568, 1109), rnd.randbytes(1568 * 1109 * 3))
    buf = io.BytesIO()
    noise.save(buf, format="JPEG", quality=30)
    pdf = pikepdf.new()
    image = pdf.make_indirect(
        pikepdf.Stream(
            pdf,
            buf.getvalue(),
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=1568,
            Height=1109,
            ColorSpace=pikepdf.Name.DeviceRGB,
            BitsPerComponent=8,
            Filter=pikepdf.Name.DCTDecode,
        )
    )
    content = pdf.make_indirect(pikepdf.Stream(pdf, b"q 842 0 0 595 0 0 cm /Im0 Do Q"))
    resources = pdf.make_indirect(pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image)))
    for _ in range(pages):
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0, 0, 842, 595], Contents=content, Resources=resources
        )
        pdf.pages.append(pikepdf.Page(page))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()
