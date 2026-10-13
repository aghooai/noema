"""`pdf.sanitize` (spec-09 §6.3, IMPL-029f): open with pikepdf, remove active content, rewrite only the objects
reachable from the new trailer, then re-scan the output. Facts: `pages`, `encrypted: false`, `active_content[]`.

Rejections: encrypted (also with an empty user password) → `encrypted_pdf`; any other open error → `corrupt`;
0 pages → `empty_content`; more than 600 → `too_many_pages`. A damaged file pikepdf opens but cannot walk, save or
re-open (missing xref objects, a name that is not UTF-8, a sanitized output without `/Root`, pikepdf's
`SystemError` on a qpdf warning that is not UTF-8) is `corrupt` too (Wave E fuzz findings), never a failed job.
A post-scan hit writes no output and the warning `sanitize_incomplete` (the worker fails the derivative and raises
the `sandbox.sanitize_incomplete` event).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of

if TYPE_CHECKING:
    import pikepdf

EXTRACTOR: Final = version_of("pikepdf")
MAX_PAGES: Final = 600
SAFE_URI_SCHEMES: Final = frozenset({"http", "https", "mailto"})
ACTION_FACTS: Final[Mapping[str, str]] = {
    "/JavaScript": "javascript",
    "/Launch": "launch",
    "/GoToR": "remote_action",
    "/GoToE": "remote_action",
    "/SubmitForm": "remote_action",
    "/ImportData": "remote_action",
    "/ResetForm": "remote_action",
    "/Rendition": "rich_media",
    "/Movie": "rich_media",
    "/Sound": "rich_media",
    "/RichMediaExecute": "rich_media",
}
ANNOT_FACTS: Final[Mapping[str, str]] = {
    "/RichMedia": "rich_media",
    "/Screen": "rich_media",
    "/Movie": "rich_media",
    "/Sound": "rich_media",
    "/3D": "rich_media",
    "/FileAttachment": "embedded_file",
}
KEY_FACTS: Final[Mapping[str, str]] = {"/AA": "additional_actions", "/JS": "javascript", "/EF": "embedded_file"}


def _is_dict(obj: Any) -> bool:
    import pikepdf

    return isinstance(obj, pikepdf.Dictionary | pikepdf.Stream)


def action_fact(action: Any) -> str | None:
    """The `active_content` value of a dangerous action dictionary, else None (`/GoTo`, `/Named`, safe `/URI`)."""
    import pikepdf

    if not _is_dict(action):
        return None
    kind = action.get("/S")
    if not isinstance(kind, pikepdf.Name):
        return None
    if str(kind) == "/URI":
        uri = action.get("/URI")
        text = str(uri) if isinstance(uri, pikepdf.String) else ""
        scheme, sep, _ = text.strip().partition(":")
        return None if sep and scheme.lower() in SAFE_URI_SCHEMES else "remote_action"
    return ACTION_FACTS.get(str(kind))


class Scanner:
    """Finds (and with `fix=True` removes) the spec-09 §6.3 active content; `facts` collects the values hit."""

    def __init__(self, pdf: pikepdf.Pdf, *, fix: bool) -> None:
        self.pdf = pdf
        self.fix = fix
        self.facts: set[str] = set()

    def _hit(self, fact: str) -> bool:
        self.facts.add(fact)
        return self.fix

    def _drop_key(self, d: Any, key: str, fact: str) -> None:
        if key in d and self._hit(fact):
            del d[key]

    def _root(self, root: Any) -> None:
        self._drop_key(root, "/OpenAction", "open_action")
        self._drop_key(root, "/NeedsRendering", "xfa")
        self._drop_key(root, "/Collection", "embedded_file")
        names = root.get("/Names")
        if _is_dict(names):
            self._drop_key(names, "/JavaScript", "javascript")
            self._drop_key(names, "/EmbeddedFiles", "embedded_file")
        form = root.get("/AcroForm")
        if _is_dict(form):
            self._drop_key(form, "/XFA", "xfa")

    def _next_chain(self, d: Any) -> None:
        import pikepdf

        nxt = d.get("/Next")
        if _is_dict(nxt):
            if (fact := action_fact(nxt)) is not None and self._hit(fact):
                del d["/Next"]
        elif isinstance(nxt, pikepdf.Array):
            keep = [a for a in nxt if (fact := action_fact(a)) is None or not self._hit(fact)]
            if self.fix and len(keep) != len(nxt):
                d["/Next"] = pikepdf.Array(keep)

    def _annots(self, d: Any) -> None:
        import pikepdf

        annots = d.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            return
        keep = []
        for annot in annots:
            subtype = annot.get("/Subtype") if _is_dict(annot) else None
            fact = ANNOT_FACTS.get(str(subtype)) if subtype is not None else None
            if fact is None or not self._hit(fact):
                keep.append(annot)
        if self.fix and len(keep) != len(annots):
            d["/Annots"] = pikepdf.Array(keep)

    def visit(self, d: Any) -> None:
        """Apply the table to one dictionary (or stream dictionary)."""
        for key, key_fact in KEY_FACTS.items():
            self._drop_key(d, key, key_fact)
        if (fact := action_fact(d.get("/A"))) is not None and self._hit(fact):
            del d["/A"]
        self._next_chain(d)
        self._annots(d)

    def walk(self) -> set[str]:
        """Visit every reachable dictionary once (the trailer, /Root and every xref object; iterative, with a visited
        set of indirect objects), the catalog rules first. Returns the facts."""
        import pikepdf

        root = self.pdf.Root
        self._root(root)
        seen: set[tuple[int, int]] = set()
        stack: list[Any] = [self.pdf.trailer, root, *self.pdf.objects]
        while stack:
            obj = stack.pop()
            if obj is None:  # a missing object of a damaged xref
                continue
            if obj.is_indirect:
                if obj.objgen in seen:
                    continue
                seen.add(obj.objgen)
            if _is_dict(obj):
                self.visit(obj)
                children = [obj.get(k) for k in list(obj.keys())]
            elif isinstance(obj, pikepdf.Array):
                children = list(obj)
            else:
                continue
            stack.extend(c for c in children if isinstance(c, pikepdf.Dictionary | pikepdf.Stream | pikepdf.Array))
        return self.facts


def _open(input_path: Path) -> pikepdf.Pdf:
    import pikepdf

    try:
        pdf = pikepdf.open(input_path, suppress_warnings=False)
    except pikepdf.PasswordError:
        raise Rejection("encrypted_pdf") from None
    except (pikepdf.PdfError, ValueError, OSError, SystemError):  # SystemError: see the module docstring
        raise Rejection("corrupt") from None
    if pdf.is_encrypted:
        pdf.close()
        raise Rejection("encrypted_pdf")
    return pdf


def _page_count(pdf: pikepdf.Pdf) -> int:
    import pikepdf

    try:
        return len(pdf.pages)
    except (pikepdf.PdfError, ValueError, KeyError):
        raise Rejection("corrupt") from None


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Sanitize one PDF (see the module docstring)."""
    import pikepdf

    damaged = (pikepdf.PdfError, TypeError, ValueError, KeyError, SystemError)  # see the module docstring

    target = out.out_dir / "pdf_sanitized.pdf"
    with _open(input_path) as pdf:
        pages = _page_count(pdf)
        if pages == 0:
            raise Rejection("empty_content")
        if pages > MAX_PAGES:
            raise Rejection("too_many_pages")
        try:
            facts = Scanner(pdf, fix=True).walk()
            pdf.save(
                target,
                deterministic_id=True,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=False,
            )
        except damaged:
            raise Rejection("corrupt") from None
    try:
        with pikepdf.open(target) as check:
            residue = Scanner(check, fix=False).walk()
            rescanned_pages = len(check.pages)
    except damaged:
        raise Rejection("corrupt") from None
    data = target.read_bytes()
    target.unlink()
    if residue or rescanned_pages != pages:
        out.warn("sanitize_incomplete")
        return
    active: list[JsonValue] = [*sorted(facts)]
    out.facts.update({"pages": pages, "encrypted": False, "active_content": active})
    out.add("pdf_sanitized.pdf", data, kind="pdf_sanitized", media_type="application/pdf")
