"""Image dimensions from the file header (PNG, JPEG, GIF, WebP) without decoding pixels (no Pillow in the runtime
dependencies). Returns None when the header is not recognized; the mock then skips the dimension rule."""

from __future__ import annotations

import struct

_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _png(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return None


def _gif(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)
    return None


def _jpeg(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(w), int(h)
        i += 2 + int(length)
    return None


def _webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return int(w) & 0x3FFF, int(h) & 0x3FFF
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the header, or None if no known header is present (or it is truncated)."""
    for probe in (_png, _jpeg, _gif, _webp):
        try:
            size = probe(data)
        except (struct.error, IndexError):
            size = None
        if size is not None:
            return size
    return None
