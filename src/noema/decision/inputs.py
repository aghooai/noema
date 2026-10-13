"""Native request input -> `DecisionInput` for `/v1/decisions` and the facade (spec-06 §4.1; IMPL-052, IMPL-074).

Uploaded `file_id` parts are checked before admission, each distinct file once (404 `not_found`, also outside
V(file); 409 `file_not_ready` / `file_rejected`), after the `MAX_MEDIA_PARTS` cap (400 `schema_violation`);
inline data-URL images are ingested after admission through `FileService.ingest_inline` (413 / 409), which waits
for the re-encoded raster only. Media parts are labeled `image-N` / `doc-N` in input order; filenames never reach
a prompt.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.decision import DecisionInput, Msg
from noema.core.protocols.media import ContentPart, FileRef, FileService
from noema.core.protocols.ops import TokenEstimator
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, JsonValue, TextBlock

from .elicitation.render import canonical_json

DATA_URL: Final = re.compile(r"^data:(?P<type>image/[A-Za-z0-9.+-]+);base64,(?P<data>.*)$", re.S)
_ESTIMATE_IMAGE: Final = ImageBlock("png", b"", 1568, 1568)  # an image at the 1,600-token cap
# Media parts (uploaded or inline) per request: the file checks run before admission, so their number is bounded
# (Wave E; the view budget renders at most 20 images and 5 documents natively, VIEW_BUDGET_V1)
MAX_MEDIA_PARTS: Final = 64
_TEXT_PARTS: Final = frozenset({"input_text", "input_json"})


def _media_label(ref: FileRef | None, image: bool) -> bool:
    if image:
        return True
    return ref is not None and (ref.media_type or ref.declared_type).startswith("image/")


class NativeInput:
    """One request's input through the file checks, the inline ingest and the conversion to `DecisionInput`."""

    def __init__(self, raw: JsonValue) -> None:
        self.raw = raw
        self._refs: dict[tuple[int, int], FileRef] = {}

    def _parts(self) -> list[tuple[int, int, Mapping[str, Any]]]:
        out: list[tuple[int, int, Mapping[str, Any]]] = []
        if isinstance(self.raw, list):
            for mi, msg in enumerate(self.raw):
                content = msg.get("content") if isinstance(msg, Mapping) else None
                for pi, part in enumerate(content if isinstance(content, list) else []):
                    if isinstance(part, Mapping):
                        out.append((mi, pi, part))
        return out

    async def check_files(
        self, files: FileService, principal: Principal, visible: Callable[[FileRef], Awaitable[None]]
    ) -> None:
        """At most `MAX_MEDIA_PARTS` media parts (400 `schema_violation`, before any lookup); every uploaded
        `file_id` must exist and pass `visible` (V(file): 404 `not_found`) and be `ready`/`partial` (409). Each
        distinct file is read and checked once."""
        parts = self._parts()
        if sum(part.get("type") not in _TEXT_PARTS for _, _, part in parts) > MAX_MEDIA_PARTS:
            raise NoemaError(
                ErrorCode.SCHEMA_VIOLATION,
                f"At most {MAX_MEDIA_PARTS} media parts per request.",
                param="input",
                details={"max_media_parts": MAX_MEDIA_PARTS},
            )
        checked: dict[str, FileRef] = {}
        for mi, pi, part in parts:
            file_id = part.get("file_id")
            if not isinstance(file_id, str):
                continue
            ref = checked.get(file_id)
            if ref is None:
                ref = await files.get(principal, file_id)
                await visible(ref)
                if ref.status == "rejected":
                    raise NoemaError(ErrorCode.FILE_REJECTED, "A file of the input was rejected.", param="input")
                if ref.status not in ("ready", "partial"):
                    raise NoemaError(ErrorCode.FILE_NOT_READY, "A file of the input is not ready yet.", param="input")
                checked[file_id] = ref
            self._refs[(mi, pi)] = ref

    async def ingest(
        self, files: FileService, principal: Principal, *, deadline: Deadline, session_id: str | None
    ) -> None:
        """Ingest every inline data-URL image (413 `image_too_large`, 409 `file_rejected` / `file_not_ready`)."""
        for mi, pi, part in self._parts():
            url = part.get("image_url")
            if not isinstance(url, str):
                continue
            m = DATA_URL.match(url)
            if m is None:  # the request gate already enforces the data-URL pattern
                raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Images must be data URLs.", param="input")
            self._refs[(mi, pi)] = await files.ingest_inline(
                principal, m.group("data"), m.group("type"), deadline=deadline, session_id=session_id
            )

    def value(self) -> DecisionInput:
        """The `DecisionInput`: text, JSON state, or messages of resolved parts (call after `ingest`)."""
        if isinstance(self.raw, str):
            return self.raw
        if isinstance(self.raw, Mapping):
            return dict(self.raw)
        images = docs = 0
        by_message: dict[int, list[ContentPart]] = {}
        for index, (mi, pi, part) in enumerate(self._parts()):
            kind = part.get("type")
            if kind == "input_text":
                cp = ContentPart(index, "text", None, text=str(part.get("text", "")))
            elif kind == "input_json":
                cp = ContentPart(index, "json", None, json=part.get("json"))
            else:
                ref = self._refs.get((mi, pi))
                image = _media_label(ref, kind == "input_image")
                images, docs = images + image, docs + (not image)
                label = f"image-{images}" if image else f"doc-{docs}"
                cp = ContentPart(index, "image" if kind == "input_image" else "file", label, file=ref)
            by_message.setdefault(mi, []).append(cp)
        raw = self.raw if isinstance(self.raw, list) else []
        return tuple(Msg("user", tuple(by_message.get(mi, ()))) for mi in range(len(raw)))

    def estimate(self, estimator: TokenEstimator, alias: str) -> int:
        """The input tokens of the work graph: text by characters, images at the 1,600-token cap, documents at
        1,500 per page (spec-04 §1 step 11)."""
        return estimator.estimate(alias, (), self._blocks())

    def _blocks(self) -> Sequence[ContentBlock]:
        if isinstance(self.raw, str):
            return (TextBlock(self.raw),)
        if isinstance(self.raw, Mapping):
            return (TextBlock(canonical_json(self.raw)),)
        out: list[ContentBlock] = []
        for mi, pi, part in self._parts():
            kind = part.get("type")
            if kind == "input_text":
                out.append(TextBlock(str(part.get("text", ""))))
            elif kind == "input_json":
                out.append(TextBlock(canonical_json(part.get("json"))))
            else:
                ref = self._refs.get((mi, pi))
                if _media_label(ref, kind == "input_image"):
                    out.append(_ESTIMATE_IMAGE)
                else:
                    out.append(DocumentBlock("pdf", "doc", b"", (ref.pages if ref and ref.pages else 1)))
        return out
