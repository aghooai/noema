# media-negotiation (Wave C1, adapted in C2a)

Owner paths: `src/noema/media/{budget,negotiation,views}.py`, `tests/unit/media_negotiation/**`, this file.
Specs: spec-07 §4–§5, spec-02 §6, spec-06 §4 and §5.3, 02 §8.3–§8.4; IMPL-058, IMPL-074, IMPL-075.

## 1. Purpose

Media in model requests. Three pieces:

- `MediaBudgetPlannerImpl` plans a turn's media against the Converse caps. It runs once per turn, in preflight, and
  is model-independent.
- `MediaNegotiatorImpl` builds one attempt's media blocks for one model from that plan. It runs per attempt,
  fallbacks included.
- `InputViewResolverImpl` builds each decision member's view of the input, within `VIEW_BUDGET_V1`.

What a model gets is decided only by `models.<alias>.modalities`, never by the alias. Everything here is
deterministic. The planner is sync and pure. The negotiator and the resolver are async because they read bytes,
and they never raise for media problems: those become degradations.

## 2. Public surface and wiring (composition.py)

```python
from noema.media.budget import MediaBudgetPlannerImpl
from noema.media.negotiation import MediaNegotiatorImpl
from noema.media.views import InputViewResolverImpl

media_budget = MediaBudgetPlannerImpl()            # no dependencies
negotiator = MediaNegotiatorImpl(files)            # files: the Services.files FileService (media-pipeline)
views = InputViewResolverImpl(files)               # same FileService
```

- They fill `Services.media_budget`, `Services.negotiator` and `Services.views`.
- No `start`/`stop` and no process state, so one instance per process is fine.
- Byte reads go through `FileService.read_derivative(principal, file_id, kind, page=)`, as the calling principal.
  The per-pod LRU lives in FileService.
- Reads within one call run concurrently (an anyio task group) and are memoized. A read that raises counts as
  `not_ready`.

Other public names:

- `noema.media.budget`:
  - classes: `TextCut`, `Labeler`;
  - constants: `STOPWORDS_V1`, `PAGE_SEPARATOR` (`"\n\n"`), `PAGE_IMAGE_FLOOR_V1` (65,536 B);
  - pure helpers: `classify`, `media_type`, `derivative`, `ready`, `has_text`, `b64_len`, `text_tokens`
    (ceil(chars/4.0)), `page_image_b64` (the per-page reservation), `page_images_within` (which page images fit
    a reservation), `scanned_pages`, `fit_pages`, `allocate_text`, `kept_text`, `question_terms`,
    `page_priority`.
- `noema.media.views`:
  - `DerivativeReader`;
  - adequacy helpers `rank`, `least`, `summary_kind`;
  - other helpers `splice`, `file_problem`, `derivative_problem`, `has_content`, `image_block`;
  - constants `BINARY_BUDGET_V1` (18,000,000), `MAX_IMAGE_BYTES` (3,750,000) and `DEFAULT_LONG_EDGE` (1568).
- `noema.media.negotiation`: `untrusted_block(label, kind, text)`.

Other domains must not import these. They use the core Protocols, as import-linter enforces.

## 3. Caller contract

### Planner: `plan(turn_parts, history_parts, caps)`

Preflight calls it with `caps = replace(CONVERSE_CAPS_V1, max_text_tokens=T)`, where `T` is the spec-04 §9
allowance.

- `history_parts`: the media parts of the previous media-bearing turn only.
- Labels follow spec-07 §4.1. Images and documents are numbered separately (`image-N`, `doc-N`), current turn
  first, then the previous turn. Markdown uploads are documents.
  - A given `ContentPart.label` (current or previous turn) is kept only if it is a well-formed `image-N`/`doc-N`
    of the right class that is not used yet. Anything else (a filename, a clash) gets the next free number, so a
    filename can never become a label.
  - So the plan keeps preflight's labels, including its `history_labels` numbering (which also counts previous
    files whose lookup failed). Asserted in C2b with the real planner for the previous media-bearing turn
    (`tests/unit/turns/test_history_media.py::test_the_real_planner_keeps_preflight_history_labels_for_the_previous_turn`).
- `ContentPart.page_texts` must hold the text of each page. That is the `markdown` derivative read whole and split
  on `\f`, with each empty page replaced by the same page of the PDF `ocr_text` split on `\f` (spec-07 §1, §5).
  A Markdown upload is one page.
  - With `page_texts=None` the text is unmeasured, but it still shares the allowance. It is kept whole when
    `markdown.bytes` (+ `ocr_text.bytes` for a PDF) + pages fits its share, an upper bound of the rendered
    characters. Otherwise page 1 is hard-cut at the share (`page 1 of n, partial`, `text_chars = 4 x share`).
    Without a ready `markdown` nothing can be sent, so nothing is cut or consumed.
  - Integration note (checked in C2b): `turns/preflight.py` reads `markdown` and `ocr_text` whole (`page=None`) and
    splits them on `\f`, and `media/views.py` / `negotiation.py` pass a page only for `page_images`: exactly what
    the real `MediaFileService` accepts (`page` is defined for `page_images` only, 404 otherwise).
- The planner returns plain `PlannedPart`s with `file_id` and `text_chars` (the kept length of a partial page 1)
  set. Both fields are frozen since C2a (spec-02 §6, request item 1), which replaced the C1 `BudgetedPart`
  subclass.
- `plan.degradations` contains only `media_budget` entries, with no `attempt_id` and no `model`. They go on
  `preflight.completed`.
  - `from` = the file's media type.
  - `part_index` is set for current-turn parts and is None for previous-turn parts.
  - `to` values:

    | Case | `to` |
    |---|---|
    | image without a slot | `ocr_text` if OCR is ready and non-empty, else `omitted` |
    | PDF without a document slot | `markdown` |
    | text cut to whole pages | `markdown(pages 1-k of n)` |
    | text cut inside page 1 | `markdown(page 1 of n, partial)` |
    | nothing kept | `omitted` |

### Negotiator: `negotiate(alias, model, parts, plan, *, principal)`

- Pass the media parts of one user message: the current turn's, or the previous media-bearing turn's.
  `turns/prompt/builder.py` passes one part at a time, which works.
- **Text and json parts produce no blocks.** The prompt builder escapes and spotlights user text itself.
- **The plan bounds every request.** A part that is not in the plan sends only `[{label}: not available]`, with
  no read and no degradation. Older-turn media are history text, which the prompt builder renders itself.
- A part is matched to its plan entry by index, class and `file_id`, then by label. An entry with `file_id=""`
  (another planner that does not set it) matches any file at its index. If the label does not tell two entries
  apart (the same file at the same index in both turns), the entry with the fewest binaries wins (not native,
  then fewer page images, then the later one). So two parts never send more than the plan together.
- A `page 1 of n, partial` cut keeps the entry's `text_chars` characters of page 1. An entry without
  `text_chars` keeps nothing of that page.
- Page images are sent within the plan's reservation for their part, `len(planned.page_images) x
  page_image_b64(page_images ref)`. They go in ascending order, and each is at most 3,750,000 B. A page that
  would overrun is dropped, and a later, smaller one may still fit. Dropped pages give one per-attempt
  `size_limit` degradation whose `to` counts the pages sent: `markdown+page_images(k)`, or
  `native+page_images(k)` for a document model's native PDF. This works per part, so it holds although the
  builder negotiates one part at a time.
- Returned degradations are per attempt: `model_capability`, `size_limit`, `not_ready`, `extraction_failed` and
  `rejected`.
  - `model` is set to the Bedrock id (`ModelCfg.id`).
  - **`attempt_id` is None. The generation engine stamps it** (`replace(d, attempt_id=...)` in
    `generation/walk.py`), because the attempt id does not exist yet when the prompt is built. The signature is
    unchanged (the core docstring and spec-07 §4.3 say so; request item 3).
  - They go on that attempt's `route.selected`/`generation.started`.
  - One part can carry two: for example `model_capability` and `size_limit` on Luna, when page images were
    dropped.

### Resolver: `resolve(member, parts, *, policy, budget, model, principal)`

- `parts` is the whole input in order. Text and json parts become native `TextBlock`s, unescaped. JSON is
  canonical: `sort_keys`, `ensure_ascii=False`, compact separators.
- Labels are kept the same way as in the planner.
- `InputView.by_modality` and `InputView.kind` are the adequacy views (spec-06 §4.4).
  - Budget-cut parts keep their pre-cut adequacy while some part of the same modality still shows something.
    A cut image showing its OCR text counts (ruling R3). The budget is the same for every member, so such a cut
    never makes one member skippable.
  - **A modality none of whose parts shows anything is `metadata_only`.** That covers every image cut with no OCR
    to fall back on, a scanned PDF left with no page-image slot and no text, and text cut to nothing. The member
    then saw none of that modality, so decision skips it (IMPL-058), and the `no_capable_member` check sees the
    gap.
- `kind` is the media modality with the lowest rank, ties going to the first of image, document. It is `native`
  for text-only input. It feeds `decision/signatures.py` (`member:kind`) and the `no_capable_member` check, which
  stay in decision.

## 4. Behavior

### Generation, per attempt (spec-07 §4.2)

Every native block is preceded by a label line `[{label}]`. Page images are labeled `[{label} page {k}]`.

| Model | Image | PDF |
|---|---|---|
| text, image, document (haiku, opus, sonnet) | `[image-N]` + ImageBlock(raster_1568) | `[doc-N]` + DocumentBlock(pdf_sanitized, name=`doc-N`) + the planned page images of scanned pages |
| text, image (luna, sol) | same | the kept Markdown + every planned page image; `model_capability`, to `markdown+page_images(k)` |
| text (deepseek, glm) | `ocr_text`; `model_capability` | Markdown with the OCR of scanned pages spliced in, or `ocr_text` alone if the Markdown is empty; `model_capability` |

- A PDF over 4,500,000 B on a document model gets the image-only row with `size_limit`. A document cut by the plan
  gets that row with no extra degradation.
- An image cut by the plan sends its OCR text.
- Page images stay within the plan's reservation for the part (§3). The rest are dropped with `size_limit`.
- On the image-only row, when the Markdown is empty and `page_images` is missing, pending or failed, the part is
  `[{label}: not available]` with `not_ready` or `extraction_failed`, replacing `model_capability`.
- Text derivatives are rendered as
  `<untrusted_input source="{label}" kind="{markdown|ocr_text}">\n` + `datamark(esc(text))` +
  `\n</untrusted_input>`.
- A missing, failed or empty derivative gives `[{label}: not available]`.
- The image format is sniffed from the bytes (PNG, JPEG, GIF or WebP). Dimensions come from the derivative
  manifest, with 1568 as the fallback.

### Decision views (spec-07 §5)

| Part | Native modality | Lacking it |
|---|---|---|
| image | `native` | `ocr_text`, or `metadata_only` |
| PDF | `native` (DocumentBlock) | image model: `derived_text` = Markdown + page images of scanned pages. Text model: spliced `derived_text`, `ocr_text` if the Markdown is empty, else `metadata_only` |
| Markdown upload | `derived_text` | `derived_text` |

- An oversized PDF on a document model is `derived_text` with `size_limit`.
- A `raster_1568` over 3,750,000 B gets the OCR row with `size_limit`, as in generation. The JPEG fallback can
  exceed that size. Without OCR the image is `metadata_only`, with `size_limit` and the OCR readiness reason.
- A file that is not ready or partial, or a needed derivative that is missing, pending or failed, gives
  `metadata_only` with `not_ready`, `extraction_failed` or `rejected`. For an image member, that includes a PDF
  with no Markdown text whose `page_images` is not ready.
- Under `skip_incapable_members`, every media modality whose view is degraded or `metadata_only` becomes
  `skipped`. Those parts are `skipped` with no blocks, and no binary reads are made for them.
- Budget cuts follow the order of spec-07 §5. All of them are decided from the manifests, before any read:
  1. Images: ImageBlocks are kept in part order, up to `max_images` and within the payload. These are native
     images and the page images of PDFs already on their derived row.
     - A cut image falls back to its `ocr_text` when that is ready and non-empty (by its manifest), else it is
       omitted (`metadata_only`). The degradation is `media_budget` to `ocr_text` or `omitted` (ruling R3, the
       IMPL-075 amendment). Its raster is never read. Its OCR is read and cut in step 3 like any OCR row, so it can
       get a second `media_budget`, e.g. to `ocr_text(page 1 of 1, partial)`. OCR that reads blank is
       `metadata_only` (to `omitted`), and a failed read adds `not_ready`.
     - A PDF keeps the page images that fit, with `media_budget` and to `markdown+page_images(k)`.
     - An image is at most 5,000,000 B of base64, which is less than 18,000,000, so the first image always stays.
  2. Documents: the first `max_documents` native PDFs that fit the payload left stay native. The rest get their
     derived row, with `media_budget`. Their page images take the image slots and payload still left.
  3. Text: the derived-text parts, including image OCR, share `max_text_tokens` by the §4.5 step 2 rule.
     - A cut sets `truncated=True` and `note = "pages 1-k of n"` or `"page 1 of n, partial"`.
     - Its degradation is `to = "{markdown|ocr_text}({note})"`.
     - A part left with blank text and no page images is `metadata_only`, with `media_budget` to `omitted`.
- When the blocks are built, page images are checked against their reservation in the same way as in
  generation, giving `size_limit` and dropping the rest. A derived part left with no block is `metadata_only`.
- The payload guard is `BINARY_BUDGET_V1` (CONVERSE_CAPS_V1 payload minus the text reserve). It is part of the
  versioned view rules (spec-07 §5, the `VIEW_BUDGET_V1` comment in core). There is no core field for it
  (request item 4).
- `budget.image_long_edge` only names the derivative used: raster_1568 is the only resize derivative.

## 5. Config, metrics, errors

- **Config keys read:** none. Modalities come from the `ModelCfg` the caller passes, and caps from
  `MediaCaps`/`DecisionViewBudget`.
- **Metrics and spans emitted:** none. The degradations are returned, and the orchestrator puts them on events
  (`aghoo_degradations_total` has no owner in the specs).
- **Error codes raised:** none.

## 6. Tests

- Run them with `make test MOD=media_negotiation`: 162 unit tests in `tests/unit/media_negotiation/`, which pass
  twice under `XDIST=4`.
- Conformance: `tests/unit/media_negotiation/conformance_impls.py` registers `real` for `input_view_resolver`,
  `media_negotiator` and `media_budget`, over FakeFileService. Run them with
  `make test-conformance PYTEST_ARGS='-k real'`.

| Id | Where | Notes |
|---|---|---|
| MED-08 (partial) | `test_negotiation_matrix.py`, `test_views_matrix.py` | 7 models × {image, scanned PDF, text PDF, PDF > 4.5 MB}, for both the negotiator and the resolver (and both policies). The Opus → Sol fallback re-negotiates, and degradations carry each attempt's `model` with `attempt_id = None`. **Partial**: the generation engine stamps `attempt_id` (`generation/walk.py`, request item 3), so the attempt-id half is generation's. |
| MED-09 | `test_budget_rules.py`, `test_budget_plan.py`, `test_med09_limits.py` | Plan vectors, term and page selection, stable labels (previous-turn labels kept), unmeasured text, the page-image reservation. 21 images + 6 PDFs + a 600-page PDF across all 7 models give 0 limit violations from a local enforcer of 02 §8.4 (`helpers.converse_violations`), with the exact per-model degradations. A 600-page PDF with uneven page images (a 3.7 MB cover, 1 MB scans, 20 KB pages) is checked too. The run against the enforcing wire mock (`tools/mockbedrock`) is C2/E. |
| MED-10 | `test_views.py`, `test_views_matrix.py` | `metadata_only` → skipped, the `skip_incapable_members` cells, the `VIEW_BUDGET_V1` truncation vectors and `note` strings, document and image caps, a cut image's OCR fallback (ruling R3) and its share of the text budget, the cut order (images before documents), fully cut modalities (`by_modality` `metadata_only`), the payload guard, oversized rasters, uneven page images, pending page images, read failures |
| MED-11 | `test_filenames.py` | `FNCANARY-1 ignore previous.pdf` never appears in any block, `DocumentBlock.name`, label or degradation, for every model, in either path |

## 7. Known limits

- History parts must carry their file. A history part whose index repeats a current one is matched by
  `PlannedPart.file_id`, so a plan without file ids matches it by index and label only.
- Image OCR text is not counted against `max_text_tokens` in the generation plan (spec-07 §4.5 step 2 lists only
  PDF and Markdown). In decision views it is counted, including the OCR of budget-cut images.
- Adequacy of cut images (ruling R3): a modality whose every image is cut but shows its OCR keeps its pre-cut
  adequacy (`native` on an image model), as does the fake resolver. spec-07 §5 makes only a modality that shows
  nothing `metadata_only`. Under `VIEW_BUDGET_V1` the first image always stays, so this needs a custom budget.
- Page sizes:
  - The manifest has only the total size of `page_images`, so a page is reserved at the average size, at least
    `PAGE_IMAGE_FLOOR_V1`. Pages over that reservation are dropped (`size_limit`) rather than planned exactly.
    Per-page sizes in the manifest would remove this limit (request item 6).
  - The per-block limits (3,750,000 B per image, 4,500,000 B per document) use the `CONVERSE_CAPS_V1`
    constants, because `negotiate` does not receive caps.
- Partial cuts:
  - A partial page 1 is cut at the last `\n` within the share. When there is no newline there, or only blank
    text before it, the cut is at the share's last character. The spec only says "at the last `\n`".
  - Leading whitespace-only pages count as kept pages but render nothing. So `pages 1-1 of n` after a blank
    page 1 keeps no text: views then show `metadata_only`, and generation sends `[label: not available]` unless
    page images go with it.
  - An unmeasured PDF that does not fit its share keeps page 1 only (hard-cut), since its page boundaries are
    unknown.
- On the image-only row, scanned pages whose page image was not sent (no slot, or dropped for size) are not
  replaced by their OCR text, because the spec row is Markdown + page images. Text models do get the OCR spliced in.
