# 07 — Media: intake, sandbox jobs, derivatives, negotiation, decision views

Wave A, lead architect. Owners:
- media-pipeline: `media/{intake,quarantine,pipeline,sandbox,extractors,worker}`, `routes/files.py`;
- media-negotiation: `media/{negotiation,budget,views}.py`.

The sandbox runtime is ADR-002 (IMPL-081). Resolutions: IMPL-029f/g, IMPL-037, IMPL-062, IMPL-065, IMPL-074, IMPL-075.

## 1. Phase 1 types and limits (03:L406-421; 02:L758-776)

| Type | Upload cap | Inline | Derivatives (job kinds) |
|---|---|---|---|
| PNG / JPEG / WebP / GIF / HEIC·HEIF | 20 MiB (20,971,520 B), ≤ 89,478,485 px | data URL ≤ 3,750,000 decoded bytes (arithmetic check, IMPL-037), long edge ≤ 8,000 px | `raster` (PNG, long edge ≤ 8,000), `raster_1568` (PNG, or JPEG if the PNG exceeds 3,750,000 B; spec-09 §6.4), `thumbnail` (≤ 256 px) via `image.normalize`; `ocr_text` via `ocr` on `raster` |
| PDF | 50 MiB (52,428,800 B, = the IMPL-064 body cap), 600 pages | — | `pdf_sanitized` (`pdf.sanitize`); then, from `pdf_sanitized`: `markdown` (`pdf.markdown`, Docling, pages separated by `\f`), `page_images` (`pdf.render`, every page at a 1568 px long edge), and `ocr_text` (`ocr` on the pages without a text layer, `\f`-separated, one segment per page of the document) |
| Markdown (`.md`) | 5 MiB (5,242,880 B) | as text parts | `markdown` (`markdown.sanitize`) |
| DOCX / SVG / audio / video | — | — | 415 `unsupported_media_type` (IMPL-062) |

The derivative kind `text` (SVG text) is unused in Phase 1.

Per request, createTurn and createDecision take at most 64 media parts (`file_id` parts plus inline images); more → 400 `schema_violation` (`param: input`, `details.max_media_parts`), before any file lookup (IMPL-064 amendment, R7).

### 1.1 File status

| Type | Required derivatives | Optional derivatives |
|---|---|---|
| image | `raster`, `raster_1568` | `thumbnail`, `ocr_text` |
| PDF | `pdf_sanitized`, `markdown` | `page_images`, `ocr_text` (only if some page has no text layer) |
| Markdown | `markdown` | — |

Derivative status is `pending → ready | failed`. File status, re-evaluated after every job result:
- `quarantined`: stored, not yet claimed. `processing`: claimed, and some required derivative is still pending.
- `rejected{code}`: `detect` or a required job returned a rejection (§1.2). Not retryable; the remaining jobs are cancelled.
- `failed{code}`: a required derivative failed (`extraction_failed`, `timeout`).
- `ready`: every derivative is `ready`.
- `partial`: every required derivative is `ready`, and some optional one is `pending` or `failed`.
- `deleted`: after `deleteFile` or retention, from any status.

`watchFile` and `wait_for_files` treat `ready`, `rejected`, `failed`, `deleted`, and `partial` with no pending derivative as terminal (IMPL-034). An extractor result is final for its derivative; there is no automatic re-run. `media.process` uses `max_attempts = 2` only to recover from a lapsed lease or a worker crash.

**Watchdog** (C2a, media-pipeline request 16a): every file gets a delayed `media.process` `{file_id, watchdog: n}` (priority 300, run after 7,200 s, `dedupe_key = <file_id>:watchdog:<n>`), armed before the `file` INSERT. A file still non-terminal when it fires is re-queued (`dedupe_key = file_id`) and the next round armed; at round 3 it is settled `failed{extraction_failed}`, so every file reaches a terminal state (MED-07).

### 1.2 Rejection and failure codes (03:L420-421)

| Condition (which job) | Code |
|---|---|
| no equivalence row matches (`detect`) | `rejected{not_allowlisted}` |
| a row matches, but the declared type, the extension, or the other detector disagrees (`detect`) | `rejected{type_mismatch}` |
| 0 bytes, a PDF with 0 pages, or a Markdown file that is empty after whitespace stripping | `rejected{empty_content}` |
| `width·height > 89,478,485` from the header, or `DecompressionBombWarning`/`DecompressionBombError` (`image.normalize`) | `rejected{pixel_limit}` |
| decompressed data over the decoder limits: Pillow `ValueError("Decompressed Data Too Large")` (PNG text and ICC chunks), a libheif security-limit error | `rejected{decompression_bomb}` |
| `UnidentifiedImageError`, a truncated image, a HEIC decode error, `pikepdf.PdfError` other than a password error | `rejected{corrupt}` |
| encrypted PDF (`pdf.sanitize`) | `rejected{encrypted_pdf}` |
| more than 600 pages (`pdf.sanitize`) | `rejected{too_many_pages}` |
| non-zero exit, OOM kill, invalid manifest, SIGSYS (ADR-002 §4, §6) | derivative `failed{extraction_failed}`; for `ocr`, `failed{ocr_failed}` |
| the nsjail or worker timeout (ADR-002 §5) | derivative `failed{timeout}` |

`file_too_large` is a synchronous 413 at upload. `duration_exceeded`, `unsupported_codec` and `malware_detected` are never produced in Phase 1.

## 2. Type detection equivalence map (IMPL-029g; 02:L821)

A file is accepted only if **one** row matches all four signals: the declared Content-Type, the lower-cased extension, the Magika label (`HIGH_CONFIDENCE` mode) and the libmagic MIME type.

| Row | Declared types | Extensions | Magika labels | libmagic MIME |
|---|---|---|---|---|
| png | image/png | .png | png | image/png |
| jpeg | image/jpeg | .jpg .jpeg | jpeg | image/jpeg |
| webp | image/webp | .webp | webp | image/webp |
| gif | image/gif | .gif | gif | image/gif |
| heic | image/heic, image/heif | .heic .heif | mp4† | image/heic, image/heif |
| pdf | application/pdf | .pdf | pdf | application/pdf |
| markdown | text/markdown, text/plain | .md .markdown | markdown, txt, unknown, any text label* | text/plain, text/markdown, text/x-diff* |

\*For Markdown, any `text/*` libmagic result is accepted when the bytes are strict UTF-8 with no NUL bytes. Magika `txt`, `unknown` and any other label whose `ContentTypeInfo.is_text` is true in the pinned model are accepted only for this row and only under the same condition (Magika gives some tiny Markdown texts other text labels, e.g. `gitattributes` for `# hi\n\nsome text\n`). The output is `markdown.sanitize`d either way (spec-09 §6.5).

†The pinned Magika (1.0.3) has no `heic`/`heif` label: it labels HEIC/HEIF (ISO-BMFF) `mp4`. libmagic (`image/heic`, `image/heif`) still has to agree, so a real MP4 video (`video/mp4`, in no row) is never accepted.

### 2.1 Synchronous pre-check (`routes/files.py`, before any byte is stored)
- Declared type = the multipart part's `Content-Type`, lower-cased, parameters (`; charset=…`) stripped. Extension = the lower-cased suffix after the last `.` of the decoded filename (RFC 7578 `filename`, else RFC 5987 `filename*`, NFC-normalized). No extension counts as a mismatch.
- The declared type must be in some row's "Declared types" and the extension in some row's "Extensions"; otherwise → 415 `unsupported_media_type` (this covers DOCX, SVG, audio and video, IMPL-062). The two needn't be in the same row yet; `detect` decides.
- Inline images (IMPL-074) have no filename. Their declared type is the data-URL type, and the extension check is skipped.
- uploadFile (C2a, request 16b): everything in the multipart body except the file part's data (part headers and the other fields) is capped at 64 KiB → 413 `request_too_large`. The extension comes from the full decoded name; `filename_display` keeps its suffix when it is cut to 255 characters.

### 2.2 Detection (`detect` job in the sandbox; the worker matches its facts)
`detect` emits `magika_label` (`PredictionMode.HIGH_CONFIDENCE`), `magika_score`, `libmagic_mime` (`python-magic`, `mime=True`, on the whole file), `bytes`, `utf8_strict` and `has_nul` (ADR-002 §5). Before that, `detect` itself rejects `type_mismatch` (a manifest rejection the validator accepts) on a polyglot signal the detectors miss (both say PNG for a PNG+PDF and PDF for a PDF+ZIP): a `%PDF-` header behind other non-whitespace bytes within the first 1024 bytes, or a ZIP end-of-central-directory record in the last 65,557 bytes (`noema.media.extractors.detect.polyglot`). The hidden-PDF signal is skipped for strict UTF-8 text without NUL (C2a, request 16d: a text file may quote `%PDF-`). The worker then:
0. `bytes == 0` → `rejected{empty_content}`.
1. `R_m` = rows whose Magika labels contain `magika_label`; `R_l` = rows whose libmagic MIME contains `libmagic_mime` (Markdown: any `text/*`). For the Markdown row, both also require `utf8_strict and not has_nul`.
2. `R_m ∩ R_l = ∅`: if `R_m` or `R_l` is empty → `rejected{not_allowlisted}`; otherwise the detectors disagree → `rejected{type_mismatch}` (a polyglot signal).
3. Otherwise the detected row `D` is the single row in `R_m ∩ R_l` (rows are disjoint). Declared type ∉ `D`, or (uploads) extension ∉ `D` → `rejected{type_mismatch}`.
4. `D` selects the per-type jobs (§3.1); `file.media_type` := the first declared type of `D`.

- B2 pins Magika and adds a unit test that every label above exists in the pinned model's `get_output_content_types()`. A missing label fails the build; it is never silently dropped.
- libmagic results are those of the media image (Debian `libmagic1`, ADR-002 §8), not the host's `file` 5.39. MED-01 runs in the image.
- Detection runs inside the sandbox. The worker only applies the steps above to the facts.

## 3. Pipelines

### 3.1 Upload (`POST /v1/files`)

1. Authz (spec-09 §3) and the §2.1 pre-check (415).
2. Stream the multipart body with the cap of the extension's row (413 `file_too_large`; partial quarantine objects are aborted) and a streaming sha256.
3. Put the bytes in quarantine at `s3://noema-<env>/<tenant>/quarantine/<file_id>`, SSE-KMS per tenant.
4. INSERT `file(status = quarantined)` and return 202 with the `FileObject`.
5. Enqueue `media.process` (priority 100, `dedupe_key = file_id`, `max_attempts = 2`).
6. Worker: claim → `processing` → run the sandbox jobs below → after each job, validate the manifest (ADR-002 §6), PUT the outputs, UPDATE the `derivative` rows, then re-evaluate the file status (§1.1).

   | Detected row | Job order (each job's input) |
   |---|---|
   | image | `detect` → `image.normalize` (original) → `ocr` (`raster`) |
   | PDF | `detect` → `pdf.sanitize` (original) → `pdf.render` and `pdf.markdown`, in parallel (`pdf_sanitized`) → `ocr` (`pdf_sanitized`, `params.pages` = the pages ∉ `text_layer_pages`; skipped if empty) |
   | Markdown | `detect` → `markdown.sanitize` (original) |

   `derivative` rows are created `pending` when the job that produces them is enqueued. A failed optional job doesn't stop the others.
7. Each status change publishes `{tenant_id, file_id, status}` on `RedisKeys.file_events(file_id)` (spec-02 §13; receivers drop a foreign `tenant_id`, spec-09 §7). `FileService.watch` wakes on it (or its 2 s poll), re-reads the row under the caller's `uow` and yields the new `FileRef`; `watchFile` streams it as a `FileObject` (IMPL-034, §3.3).
8. Uploads named in a turn or decision must be `ready` or `partial` (409 `file_not_ready` / `file_rejected`), unless `wait_for_files`.

### 3.2 Inline images (IMPL-074)

`FileService.ingest_inline(principal, data_b64, media_type, *, deadline)`:
1. Arithmetic size check → 413 `image_too_large`.
2. Base64-decode. This is byte-level only, not a media decode.
3. sha256; quarantine put; `file(origin = inline, session_id)`.
4. Enqueue `media.process` with **priority 10**. `JobQueue` wakes workers with `LISTEN/NOTIFY` (`pg_notify('jobs', kind)`), so there is no poll latency.
5. Await the `image.normalize` result (`raster` and `raster_1568` come from the same job), bounded by `min(deadline, now + platform.media.inline_wait_ms)`. `detect` and `image.normalize` (of the declared format) run in parallel, and the outputs are stored only after `detect` matched (C2a, request 16c; IMPL-074 latency):
   - ready, original long edge ≤ 8,000 → return the `FileRef`; `ocr` continues in the background;
   - the manifest's original long edge > 8,000 px → 413 `image_too_large` (the file is marked `rejected{pixel_limit}`, no derivative is stored);
   - a rejection (§1.2) → 409 `file_rejected` (`details.rejection`); a failure → 409 `file_rejected` (`details.failure`) (IMPL-074);
   - timeout → 409 `file_not_ready`, retryable.
   - Equivalence for inline: the data-URL type is the declared type; there is no extension check (§2.1).
6. The inline file expires with the decision or turn, per the data-class retention.

The API process never parses media (ADR-002 §1). The latency budget (target p95 < 400 ms for a 3.75 MB JPEG) is measured in the C2 perf smoke.

### 3.3 `FileObject` mapping (`routes/files.py`; openapi `FileObject`, IMPL-034)

| Field | Source |
|---|---|
| `id`, `bytes`, `sha256`, `status`, `rejection`, `failure` | the `file` row |
| `filename` (required) | `filename_display`; inline files have none → `"{file_id}.{ext}"`, `ext` from the declared type's row (`png`, `jpg`, `webp`, `gif`, `heic`) |
| `media_type` (required) | `file.media_type`, or `declared_type` while it is NULL (before `detect`, spec-03 §2) |
| `derivatives[]` | the `derivative` rows except `raster_1568`, ordered by kind as in the openapi `DerivativeKind` enum; `pages` only on `page_images` |

## 4. Generation negotiation (`media/negotiation.py`; 02 §8.3, §8.4)

### 4.1 Labels (`doc-N`, `image-N`)
- Images and documents are numbered separately, 1-based: first the current turn's parts in part order, then the previous media-bearing turn's, then older turns' text derivatives. The decision path labels the current message the same way, so `doc-1` is the same file in decision and generation prompts.
- The planner keeps a previous-turn part's given label when it is well-formed and unused, so the plan's labels equal preflight's `history_labels` (preflight numbers the history after the current turn, so nothing clashes); C2 can assert that equality for the previous media-bearing turn.
- Labels are fixed by the plan, so every attempt of a turn uses the same ones. `DocumentBlock.name` = the label: it satisfies Converse's name rule (alphanumerics, single spaces, hyphens, parentheses, brackets; ≤ 200 chars). Filenames never appear (spec-09 §5).

### 4.2 Matrix (02 §8.3; decided only by `models.<alias>.modalities`, never by the alias)
`await MediaNegotiator.negotiate(alias, model, parts, plan, principal=...)` (async: it reads the derivative bytes with `FileService.read_derivative`; spec-02 §6) re-runs for every attempt, fallbacks included, through `PromptBuilder.build(alias)` (spec-02 §10). `InputViewResolver` (§5) gives decision members the same representation, within `VIEW_BUDGET_V1`.

| Model (yaml modalities) | Image | PDF | 02 §8.3 |
|---|---|---|---|
| haiku-5.5 (text, image, document) | `ImageBlock(raster_1568)` | `DocumentBlock(pdf_sanitized)` + `page_images` of scanned pages | native / native |
| opus-5.5 (text, image, document) | same | same | native / native |
| sonnet-5.5 (text, image, document; UNVERIFIED, IMPL-043) | same | same | — (labeler) |
| luna-6 (text, image) | `ImageBlock(raster_1568)` | `markdown` + `page_images` of the planned pages; `model_capability` | native / Markdown + page images |
| sol-6.1 (text, image) | same | same | native / Markdown + page images |
| deepseek-3.2 (text) | `ocr_text`; `model_capability` | `markdown`, with the `ocr_text` segments of scanned pages spliced in at their page positions; `ocr_text` alone if the Markdown is empty; `model_capability` | `ocr_text` / Markdown or `ocr_text` |
| glm-5.3 (text) | same | same | `ocr_text` / Markdown or `ocr_text` |

- A PDF `DocumentBlock` is skipped (→ the `image`-only row, degradation `size_limit`) when `pdf_sanitized.bytes > 4,500,000`, or when the plan didn't give it a document slot (`media_budget`).
- Text derivatives are rendered as `<untrusted_input source="{label}" kind="{markdown|ocr_text}">` blocks with datamarked bodies in every attempt (standard spotlighting, IMPL-086; spec-09 §5.3). A label text block `[{label}]` precedes each native block in the same message (Converse needs a text block next to a document).
- A text derivative that is missing (`not_ready`), failed (`extraction_failed`) or empty → the part is omitted and the label line `[{label}: not available]` is sent.
- Native image and document blocks are only ever placed in `user` messages: the current one, or the previous media-bearing turn's user message (02:L797).

### 4.3 Degradations (types.ts `Degradation`)
`from` = the file's `media_type`; `to` is one of `ocr_text`, `markdown`, `markdown+page_images(<k>)`, `markdown(pages 1-<k> of <n>)`, `native+page_images(<k>)` (a native PDF whose scanned page images were partly dropped, §4.5; k = sent), `omitted`.
- Per-attempt degradations (`model_capability`, `size_limit`, `not_ready`, `extraction_failed`) carry `attempt_id` and `model`, and go on that attempt's `route.selected`/`generation.started`. `negotiate` sets `model` (the Bedrock id) and leaves `attempt_id` None, because the attempt id does not exist when the prompt is built; the generation engine stamps it (`dataclasses.replace(d, attempt_id=...)`).
- Plan cuts (`media_budget`) and `active_content_removed` carry neither, and go on `preflight.completed`.
- Older-turn media in history are text by design (IMPL-045). They are not degradations.

### 4.4 Planner types (a copy of the frozen text in spec-02 §6, `core/protocols/media.py`)

```python
@dataclass(frozen=True, slots=True)
class MediaCaps:                         # Converse limits per request, 02 §8.4 [MM]
    max_images: int = 20; max_image_bytes: int = 3_750_000
    max_documents: int = 5; max_document_bytes: int = 4_500_000
    max_payload_bytes: int = 20_000_000; text_reserve_bytes: int = 2_000_000   # text, JSON framing, system prompt
    max_text_tokens: int = 0             # supplied by preflight: the IMPL-045 allowance for derived text
CONVERSE_CAPS_V1: Final = MediaCaps()
@dataclass(frozen=True, slots=True)
class PlannedPart:
    part_index: int; label: str; origin: Literal["current", "previous"]; kind: Literal["image", "pdf", "markdown"]
    native: bool                          # image: an ImageBlock slot; pdf: a DocumentBlock slot
    text_pages: int                       # pdf/markdown: pages 1..text_pages of the text kept (0 = none)
    text_cut: str | None                  # "pages 1-k of n" or "page 1 of n, partial"; None if whole
    page_images: tuple[int, ...]          # pdf: pages that may be sent as images, ascending
    file_id: str = ""                     # C2a: finds a previous-turn part whose part_index repeats a current one
    text_chars: int | None = None         # C2a: kept length of page 1 for "page 1 of n, partial"
@dataclass(frozen=True, slots=True)
class MediaPlan:
    parts: tuple[PlannedPart, ...]; degradations: tuple[Degradation, ...]   # media_budget only
```

### 4.5 Planner algorithm (`media/budget.py`, sync, deterministic; 02 §8.4)
`plan(turn_parts, history_parts, caps)`. `history_parts` holds the media parts of the previous media-bearing turn only; older media reach the prompt as history text (IMPL-045). Sizes: `b64(n) = 4·ceil(n/3)`. A page image is reserved at `page_image_b64 = b64(min(max_image_bytes, max(ceil(page_images.bytes / pages), PAGE_IMAGE_FLOOR_V1)))`, `PAGE_IMAGE_FLOOR_V1 = 65,536` (`noema.media.budget.page_image_b64`; real pages differ, a scanned page can be 50× a text page). The binary budget is `max_payload_bytes − text_reserve_bytes`.

1. For `origin` in (current, previous), in part order:
   1. Each image: `native` iff an image slot is left and `b64(raster_1568.bytes)` fits the binary budget. Otherwise → `media_budget`, `to` = `ocr_text` if it is ready and non-empty, else `omitted`.
   2. Each PDF with `pdf_sanitized.bytes ≤ max_document_bytes`: `native` iff a document slot is left and `b64(bytes)` fits. Otherwise (count or payload) → `media_budget`, `to: markdown`. Oversized PDFs take no slot; the negotiator reports `size_limit` per attempt.
   3. Page images: the PDFs of this origin in order. Each gets `r = floor(image slots left / PDFs of this origin left)`. Its selection is the first `r` distinct pages of: pages 1-2; then pages matching the question terms (more distinct terms first, then lower page); then pages without a text layer; then the remaining pages in order. Sorted ascending, while each page fits the binary budget. Terms = `{w for w in re.findall(r"\w{4,}", current_user_text.casefold())} − STOPWORDS_V1` (a code constant). A page matches if a term is one of its `\w+` tokens: the Markdown segment, else the OCR segment.
2. Text: the derived items (every PDF and Markdown part, current first) share `max_text_tokens`. Each takes `floor(remaining / items left)` and keeps whole pages from page 1 while they fit (`TokenEstimator`, the default chars/4 rule, so it is model-independent). If not even page 1 fits, page 1 is cut at the last `\n` within the share, or hard-cut at the share when there is no such newline or only blank text before it, so a non-zero share always keeps text (`text_cut = "page 1 of n, partial"`). Any cut → `media_budget`, `to: markdown(pages 1-k of n)`. The unused share passes to the next item. A Markdown upload is one page. An item without `page_texts` (not read by preflight) is kept whole when `markdown.bytes` (+ `ocr_text.bytes` for a PDF) + `pages`, an upper bound of its rendered characters, fits the share by the same chars/4 rule; otherwise page 1 is hard-cut at the share (`page 1 of n, partial`). A part that is not in the plan sends only `[{label}: not available]`.
3. The negotiator applies the plan per model (§4.2): document models send `DocumentBlock` for native PDFs plus `page_images ∩ scanned pages`; image-only models send the kept Markdown plus all `page_images`; text-only models send the kept text. Page images stay within the plan's reservation: the negotiator (per part, so this holds although the prompt builder negotiates one part at a time) and the resolver (§5) send a part's page images in ascending order, each ≤ `max_image_bytes` and all within `len(planned page_images) × page_image_b64`; the others are dropped with a per-attempt `size_limit`.

Because every allocation checks the worst case for both PDF representations, no attempt of any model can exceed `MediaCaps`. MED-09 asserts this against the enforcing wire mock.

## 5. Decision input views (`media/views.py`; spec-06 §4, IMPL-058, IMPL-075)

`InputViewResolver.resolve(member, parts, *, policy, budget=VIEW_BUDGET_V1, model, principal)` (spec-02 §6; `model` = the member's `ModelCfg`, `principal` = the caller, whose `uow` scopes the derivative reads) decides by `models.<alias>.modalities` (§4.2 rows). It returns content blocks and metadata only; decision renders the label lines, the wrapping and the escaping (IMPL-050).

| Part | Model has the native modality | Model lacks it (`derive_for_text_members`) |
|---|---|---|
| text / json | `native` (TextBlock) | — |
| image | `native` (ImageBlock `raster_1568`) | `ocr_text` if ready and non-empty, else `metadata_only` |
| PDF | `document`: `native` (DocumentBlock `pdf_sanitized`, ≤ 4,500,000 B). `image` only, or an oversized PDF: `derived_text` = Markdown + page images of the pages without a text layer | text only: `derived_text` = Markdown with the OCR segments of scanned pages spliced in by page; `ocr_text` if the Markdown is empty; `metadata_only` if both are empty or not ready |

- `skip_incapable_members`: a member whose view of some modality would be degraded (`ocr_text`) or `metadata_only` is `skipped` instead (`input_view: skipped`). For a PDF that means the text-only `ocr_text` case only, because `derived_text` is adequate (02:L341-350).
- A derivative the view needs that isn't `ready` → `metadata_only`, with degradation `not_ready` (`extraction_failed` if it failed). A `metadata_only` member is `skipped` (`skip_reason: input_view`, IMPL-058).
- `InputView.kind` is the least adequate modality (spec-06 §4.4). Phase 1 never emits `thumbnail`.
- Page segments: `markdown` and PDF `ocr_text` derivatives hold one `\f`-separated segment per page (§1). The resolver splits on `\f` before any text processing (`clean_text` would remove `\f`).

`VIEW_BUDGET_V1` cut rule (IMPL-075), applied in this order. The current message comes first by construction (spec-06 §4.1). Native images, documents and page images also stay within the binary budget `CONVERSE_CAPS_V1.max_payload_bytes − text_reserve_bytes` (18,000,000 B of base64) and are cut beyond it exactly like the count caps; below it nothing changes (20 images of 3.75 MB plus 5 PDFs of 4.5 MB would exceed the ~20 MB Converse payload, 02 §8.4).
1. Images: ImageBlocks are kept in part order up to `max_images` (20): native images, then the page images of derived rows (each part's page images as in §4.5 step 3). A cut image → its `ocr_text` when that is ready and non-empty, else omitted (`metadata_only`, no blocks); degradation `media_budget` to `ocr_text` / `omitted` (orchestrator ruling R3, 2026-10-10).
2. Documents: the first `max_documents` (5) native documents (count and binary budget) are kept; the rest get `derived_text`, whose page images take the image slots and payload that step 1 left.
3. Text: the derived text parts share `max_text_tokens` (6,000, `TokenEstimator`) in part order, using the §4.5 step 2 rule (equal shares, unused share passed on, whole pages from page 1). A cut part gets `ViewPart.truncated = True` and `ViewPart.note = "pages 1-k of n"` (ASCII hyphen), or `"page 1 of n, partial"`. Decision renders `[doc-N: derived_text; pages 1-k of n]` (spec-06 §5.3); the resolver never emits label text. The partial page-1 cut is the §4.5 step 2 one (a hard cut when the last newline has only blank text before it).

- Adequacy after the cuts (`by_modality`, `kind`): a budget-cut part keeps its pre-cut adequacy while another part of the same modality still shows something; a modality none of whose parts shows anything (every image cut, a scanned PDF with no image slot and no text, text cut to nothing) is `metadata_only`, so IMPL-058 skips the member.
- A `raster_1568` over 3,750,000 B (the JPEG fallback can be) gets the OCR row with `size_limit`, as in generation. For an image-only member, a PDF with no Markdown text whose `page_images` are missing, pending or failed is `metadata_only` with `not_ready` / `extraction_failed`.

Every cut is reported in `InputView.degradations` (`media_budget`, the §4.3 `to` grammar). Changing `VIEW_BUDGET_V1`, or any rule in this section, requires new profile versions (IMPL-075).

## 6. Derivative download (IMPL-065)

`GET /v1/files/{id}/derivatives/{kind}[?page=n]` (authz spec-09 §3, `file.derivative.read`):
- streams from S3 with the per-kind Content-Type: `raster`, `thumbnail`, `page_images` → `image/png`; `pdf_sanitized` → `application/pdf`; `markdown` → `text/markdown; charset=utf-8`; `ocr_text` → `text/plain; charset=utf-8`;
- `raster_1568` is internal (model input only): it is not an openapi `DerivativeKind` value, so `…/derivatives/raster_1568` fails the request gate (400 `schema_violation`), and it never appears in `FileObject.derivatives` (§3.3);
- `Content-Disposition: attachment; filename="<file_id>-<kind>[-<page>].<ext>"`, never the uploaded name;
- `X-Content-Type-Options: nosniff`;
- `page` is required for `page_images` (1 ≤ page ≤ `pages`, else 400 `schema_violation`) and rejected for other kinds; a missing kind → 404 `not_found`; pending → 409 `file_not_ready`; a rejected file → 409 `file_rejected`.

The UI renders images only through blob URLs, never embeds PDFs, and never renders Markdown derivatives as HTML (spec-09 §6.8, §8).

## 7. Tests (owners: media-pipeline MED-01…07, media-negotiation MED-08…11)

Tests that need tesseract, nsjail, qpdf or the pinned libmagic run in the media image (ADR-002 §8; the host lacks them). Under `BwrapRunner` on the host they are skipped, with the reason given.
- **MED-01** equivalence map: each row accepted (with and without `; charset`); each cross-row declared/extension mismatch → `type_mismatch`; Magika ≠ libmagic → `type_mismatch`; no row → `not_allowlisted`; DOCX/SVG/MP3/MP4 → 415 before any storage; polyglots (PDF+ZIP, PNG+PDF) rejected; Markdown with NUL or invalid UTF-8 rejected; every Magika label is present in the pinned model.
- **MED-02** golden derivatives from `/codes/aghooai/probes/fixtures/`: `number.png` → OCR contains "4471"; `invoice.pdf` → `pdf_sanitized`, `markdown`, `page_images` with `pages = 1`.
- **MED-03** images: an HEIC → PNG fixture, generated by the test from a PNG with pillow-heif in the media image; an EXIF-rotated JPEG → upright pixels and no EXIF/XMP/ICC in any output; animated GIF/WebP → frame 0; 9,500 × 9,500 (90,250,000 px > 89,478,485) → `pixel_limit`; an upload with an 8,200 px long edge within the pixel limit → accepted, `raster` downscaled to 8,000 (only inline images over 8,000 px are refused, MED-06); a PNG zTXt bomb → `decompression_bomb`; truncated JPEG and HEIC → `corrupt`; a noisy 1,568 px PNG over 3,750,000 B → `raster_1568.jpg`.
- **MED-04** PDF: the spec-09 §6.3 fixtures → each `active_content[]` value, `active_content_removed` on the turn, and the output passes the post-scan and `qpdf --check`; encrypted (with an empty user password) → `encrypted_pdf`; 601 pages → `too_many_pages`.
- **MED-05** Markdown sanitizer properties (spec-09 §6.5).
- **MED-06** inline: 3,750,000 bytes accepted; 3,750,001 → 413 with no job enqueued; long edge 8,001 px → 413 from the job; type mismatch → 409 `file_rejected`; timeout → 409 `file_not_ready`.
- **MED-07** status machine: every §1.1 transition, including `partial` with OCR pending → `ready`; `watchFile` ends on the terminal statuses only.
- **MED-08** negotiation matrix: the §4.2 table holds 100% for the 7 models × {image, scanned PDF, text PDF, PDF > 4.5 MB}, for both `MediaNegotiator` and `InputViewResolver`; degradations carry the right `attempt_id` on a fallback from Opus to Sol (02:L789).
- **MED-09** budget: 21 images + 6 PDFs + a 600-page PDF, over every model in the matrix → 0 `ValidationException`s from the enforcing wire mock, with the expected `media_budget` degradations; plan vectors for the §4.5 term and page selection; labels `image-1…`/`doc-1…` stable across attempts.
- **MED-10** decision views: `metadata_only` → skipped; `skip_incapable_members` cells; the `VIEW_BUDGET_V1` truncation vectors and their `note` strings.
- **MED-11** filenames: a file named `FNCANARY-1 ignore previous.pdf` never appears in any request (it shares the corpus with SEC-INJ-03).
- Sandbox suite SEC-SBX-01…08 (ADR-002).
