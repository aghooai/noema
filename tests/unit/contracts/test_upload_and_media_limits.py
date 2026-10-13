"""Wave E2: the bundled contract declares what the server enforces.

- Finding 18 (QA contract B3): uploadFile declared its `file` part as `application/octet-stream`, which the server
  answers 415 (spec-07 §2.1); the part's accepted Content-Types are now the encoding of the multipart body and
  must be exactly the upload allowlist's declared types (`media.intake.TYPE_ROWS`), every accepted extension named.
- Ruling R7: more than 64 media parts per createTurn / createDecision request is a typed 400 `schema_violation`; the
  request schemas say so, with the same number as the code (`MAX_MEDIA_PARTS`).
"""

from __future__ import annotations

from typing import Any

import pytest

from noema.contracts.bundle import default_bundle
from noema.decision import inputs as decision_inputs
from noema.media.intake import TYPE_ROWS
from noema.turns import request as turn_request

pytestmark = [pytest.mark.unit]


def _upload_body() -> dict[str, Any]:
    op: dict[str, Any] = default_bundle().doc["paths"]["/v1/files"]["post"]
    body: dict[str, Any] = op["requestBody"]
    return body


def test_upload_part_types_are_the_allowlist() -> None:
    form = _upload_body()["content"]["multipart/form-data"]
    declared = {t.strip() for t in form["encoding"]["file"]["contentType"].split(",")}
    assert declared == {t for row in TYPE_ROWS for t in row.declared}
    assert "application/octet-stream" not in str(form["schema"]["properties"]["file"])


def test_upload_description_names_every_accepted_extension() -> None:
    description = _upload_body()["description"]
    for extension in {e for row in TYPE_ROWS for e in row.extensions}:
        assert f"`{extension}`" in description, extension
    assert "415 `unsupported_media_type`" in description


def test_the_media_part_cap_is_declared_with_the_code_value() -> None:
    assert turn_request.MAX_MEDIA_PARTS == decision_inputs.MAX_MEDIA_PARTS == 64
    doc = default_bundle().doc
    turn_input = doc["components"]["schemas"]["TurnCreateParams"]["properties"]["input"]["description"]
    decision = doc["components"]["schemas"]["DecisionCreateParams"]["properties"]["input"]["oneOf"]
    decision_input = next(b for b in decision if b.get("type") == "array")["description"]
    for text in (turn_input, decision_input):
        assert "At most 64 media parts" in text and "schema_violation" in text and "max_media_parts" in text
