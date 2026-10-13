"""MED-01 (spec-07 §2, IMPL-029g, IMPL-062): the equivalence map, the synchronous 415 pre-check, the upload caps and
the inline arithmetic size check (IMPL-037)."""

from __future__ import annotations

import base64

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue
from noema.media import intake
from noema.media.intake import Detected, Rejected, match_detection, precheck_upload

pytestmark = pytest.mark.unit

ROW_SAMPLES = [  # (row, declared, extension, magika label, libmagic mime)
    ("png", "image/png", ".png", "png", "image/png"),
    ("jpeg", "image/jpeg", ".jpg", "jpeg", "image/jpeg"),
    ("jpeg", "image/jpeg", ".jpeg", "jpeg", "image/jpeg"),
    ("webp", "image/webp", ".webp", "webp", "image/webp"),
    ("gif", "image/gif", ".gif", "gif", "image/gif"),
    ("heic", "image/heic", ".heic", "mp4", "image/heic"),
    ("heic", "image/heif", ".heif", "mp4", "image/heif"),
    ("pdf", "application/pdf", ".pdf", "pdf", "application/pdf"),
    ("markdown", "text/markdown", ".md", "markdown", "text/plain"),
    ("markdown", "text/plain", ".markdown", "txt", "text/x-diff"),
    ("markdown", "text/markdown", ".md", "unknown", "text/x-python"),  # any text/* for valid UTF-8 without NUL
]


def facts(label: str, mime: str, *, size: int = 10, utf8: bool = True, nul: bool = False) -> dict[str, JsonValue]:
    return {
        "magika_label": label,
        "magika_score": 0.99,
        "libmagic_mime": mime,
        "bytes": size,
        "utf8_strict": utf8,
        "has_nul": nul,
    }


@pytest.mark.parametrize(("row", "declared", "ext", "label", "mime"), ROW_SAMPLES)
@pytest.mark.parametrize("charset", ["", "; charset=utf-8"])
def test_each_row_is_accepted(row: str, declared: str, ext: str, label: str, mime: str, charset: str) -> None:
    pre = precheck_upload(declared.upper() + charset, f"Some File{ext.upper()}")
    assert pre.extension == ext and pre.declared == declared
    result = match_detection(facts(label, mime), declared + charset, ext)
    assert isinstance(result, Detected) and result.row.name == row


def test_cross_row_declared_or_extension_mismatch_is_type_mismatch() -> None:
    png = facts("png", "image/png")
    for declared, ext in (("image/jpeg", ".png"), ("image/png", ".jpg"), ("application/pdf", ".pdf")):
        precheck_upload(declared, f"a{ext}")  # the pre-check passes: rows may differ until detect decides
        assert match_detection(png, declared, ext) == Rejected("type_mismatch")


def test_detector_disagreement_is_type_mismatch_and_no_row_is_not_allowlisted() -> None:
    assert match_detection(facts("png", "application/pdf"), "image/png", ".png") == Rejected("type_mismatch")
    assert match_detection(facts("zip", "application/zip"), "application/pdf", ".pdf") == Rejected("not_allowlisted")
    assert match_detection(facts("pdf", "application/zip"), "application/pdf", ".pdf") == Rejected("not_allowlisted")
    # an MP4 video named .heic: Magika says mp4, libmagic video/mp4 -> no libmagic row
    assert match_detection(facts("mp4", "video/mp4"), "image/heic", ".heic") == Rejected("not_allowlisted")


def test_markdown_needs_strict_utf8_without_nul() -> None:
    for kwargs in ({"utf8": False}, {"nul": True}):
        result = match_detection(facts("txt", "text/plain", **kwargs), "text/markdown", ".md")
        assert result == Rejected("not_allowlisted")


def test_markdown_accepts_any_magika_text_label() -> None:
    """spec-07 §2 Markdown row (media-pipeline request item 10): Magika labels some tiny notes `gitattributes`."""
    for label in ("gitattributes", "python", "html"):
        result = match_detection(facts(label, "text/plain"), "text/markdown", ".md")
        assert isinstance(result, Detected) and result.row.name == "markdown"
        assert match_detection(facts(label, "text/plain", nul=True), "text/markdown", ".md") == Rejected(
            "not_allowlisted"
        )
        assert match_detection(facts(label, "text/plain"), "image/png", ".png") == Rejected("type_mismatch")
    assert match_detection(facts("zip", "text/plain"), "text/markdown", ".md") == Rejected("not_allowlisted")
    other_rows = {label for row in intake.TYPE_ROWS if row.kind != "markdown" for label in row.magika}
    assert not other_rows & intake.MAGIKA_TEXT_LABELS  # the rows stay disjoint


def test_empty_file_is_empty_content() -> None:
    assert match_detection(facts("empty", "application/x-empty", size=0), "image/png", ".png") == Rejected(
        "empty_content"
    )


def test_inline_images_skip_the_extension_check() -> None:
    assert isinstance(match_detection(facts("gif", "image/gif"), "image/gif", None), Detected)
    assert intake.inline_row("IMAGE/JPEG; q=1").name == "jpeg"
    with pytest.raises(NoemaError) as err:
        intake.inline_row("application/pdf")
    assert err.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE


@pytest.mark.parametrize(
    ("declared", "filename"),
    [
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        ("image/svg+xml", "a.svg"),
        ("image/png", "a.svg"),
        ("audio/mpeg", "a.mp3"),
        ("video/mp4", "a.mp4"),
        ("image/png", "noextension"),
        ("image/png", "trailingdot."),
        ("application/octet-stream", "a.png"),
    ],
)
def test_phase2_and_unknown_types_are_415_before_storage(declared: str, filename: str) -> None:
    with pytest.raises(NoemaError) as err:
        precheck_upload(declared, filename)
    assert err.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE


def test_caps_follow_the_extension_row() -> None:
    assert precheck_upload("image/png", "a.png").cap_bytes == 20_971_520
    assert precheck_upload("application/pdf", "a.PDF").cap_bytes == 52_428_800
    assert precheck_upload("text/markdown", "notes.md").cap_bytes == 5_242_880
    assert precheck_upload("application/pdf", "a.md").cap_bytes == 5_242_880  # the extension's row decides


def test_extension_is_nfc_and_path_free() -> None:
    assert intake.extension_of("C:\\dir\\photo.JPG") == ".jpg"
    assert intake.extension_of("archive.tar.gz") == ".gz"
    assert intake.extension_of("Cafe\u0301.md") == ".md"


def test_a_long_name_keeps_its_extension() -> None:
    """spec-07 §2.1 takes the extension from the decoded name; only the display name is cut (C1 review)."""
    long_name = "a" * 300 + ".png"
    params = {"filename": long_name}
    assert intake.decoded_filename(params) == long_name
    assert precheck_upload("image/png", long_name).extension == ".png"
    shown = intake.display_filename(params)
    assert shown is not None and len(shown) == 255 and shown.endswith("a.png")
    assert intake.shorten_filename("b" * 300) == "b" * 255
    assert len(intake.shorten_filename("c." + "x" * 300)) == 255  # a suffix over 32 characters is not kept


def test_inline_size_is_arithmetic() -> None:
    exact = base64.b64encode(b"\x00" * 3_750_000).decode()
    assert intake.inline_decoded_size(exact) == 3_750_000
    intake.check_inline_size(exact)
    over = base64.b64encode(b"\x00" * 3_750_001).decode()
    with pytest.raises(NoemaError) as err:
        intake.check_inline_size(over)
    assert err.value.code is ErrorCode.IMAGE_TOO_LARGE
    assert intake.inline_decoded_size("QQ==") == 1 and intake.inline_decoded_size("QUI=") == 2


def test_invalid_base64_is_a_corrupt_rejection() -> None:
    with pytest.raises(NoemaError) as err:
        intake.decode_inline("not base64!")
    assert err.value.code is ErrorCode.FILE_REJECTED
    assert err.value.details == {"rejection": {"code": "corrupt", "message": intake.REJECTION_MESSAGES["corrupt"]}}


def test_every_magika_label_exists_in_the_pinned_model() -> None:
    magika = pytest.importorskip("magika")
    labels = {str(t) for t in magika.Magika().get_output_content_types()}
    missing = {label for row in intake.TYPE_ROWS for label in row.magika} - labels
    assert not missing, f"Magika labels missing from the pinned model: {missing}"
    # spec-07 §2 lists heic/heif; the pinned model has neither (media-pipeline request item 1)
    assert not {"heic", "heif"} & labels
    model = magika.Magika()
    texts = {str(t) for t in model.get_output_content_types() if model._get_ct_info(t).is_text}
    assert texts == intake.MAGIKA_TEXT_LABELS  # the constant is the pinned model's is_text set
