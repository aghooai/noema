"""Write the W2 upload files for the Playwright specs into <out_dir> (from the repo root, in the project venv).

The files come from the media pipeline's generated fixtures (tests/unit/media_pipeline/media_fixtures.py), so the stack
sees the same PDFs as MED-04: a text invoice, one with an OpenAction script (active content), an encrypted one, a PNG,
and a .docx name (415 before any storage, IMPL-062).
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.unit.media_pipeline.media_fixtures import encrypted_pdf, malicious_pdfs, png, text_pdf


def main(argv: list[str]) -> int:
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "invoice.pdf": text_pdf("Invoice number 4471", pages=2),
        "active-content.pdf": malicious_pdfs()["open_action"][0],
        "encrypted.pdf": encrypted_pdf(),
        "photo.png": png(64, 48),
        "notes.docx": b"PK\x03\x04 not really a document",
    }
    for name, data in files.items():
        (out / name).write_bytes(data)
    sys.stdout.write(f"wrote {len(files)} files to {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
