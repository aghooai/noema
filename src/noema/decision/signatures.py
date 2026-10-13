"""Question signatures, the per-tenant input digest, the view signature and the option order (spec-06 §3.3-§3.6;
IMPL-051, IMPL-059).

Everything here is pure and byte-exact: the vectors of spec-06 §3 and §17 T-SIG-1 are unit tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from collections.abc import Mapping, Sequence
from typing import Final

from noema.core.protocols.decision import DecisionInput, Msg, VerificationInput
from noema.core.protocols.media import ContentPart

DIGEST_CONTEXT: Final = b"noema/input-digest/v1/"
PEPPER_REF: Final = "noema/input-digest-pepper"  # SecretsProvider ref of the 32-byte pepper (spec-06 §1.2)


def canon(obj: object) -> bytes:
    """Canonical JSON of the question signatures: sorted keys, compact separators, ensure_ascii (spec-06 §3.3)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_plain).encode()


def question_signature(wire_q: Mapping[str, object]) -> str:
    """`sha256:` + hex of the question as validated (no defaults added, no synonyms normalized)."""
    return "sha256:" + hashlib.sha256(canon(wire_q)).hexdigest()


def question_set_signature(wire_qs: Sequence[Mapping[str, object]]) -> str:
    """`sha256:` + hex of the list of question dicts in answer order."""
    return "sha256:" + hashlib.sha256(canon(list(wire_qs))).hexdigest()


def tenant_digest_key(pepper: bytes, tenant_id: str) -> bytes:
    """`K_t = HMAC(pepper, "noema/input-digest/v1/" + tenant_id)`."""
    return hmac.new(pepper, DIGEST_CONTEXT + tenant_id.encode(), "sha256").digest()


def input_digest(tenant_key: bytes, value: DecisionInput) -> str:
    """`hmac-sha256:` + HMAC(K_t, canonical_input_bytes(input)) (spec-06 §3.4)."""
    return "hmac-sha256:" + hmac.new(tenant_key, canonical_input_bytes(value), "sha256").hexdigest()


def canonical_input_bytes(value: DecisionInput) -> bytes:
    """A string byte-exact; everything else as canonical JSON (sorted keys, compact, ensure_ascii=False)."""
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        canonical_input(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_plain
    ).encode("utf-8")


def canonical_input(value: DecisionInput) -> object:
    """The JSON structure the digest hashes: JSON state as is; messages with media parts replaced by their content
    hashes (and the ready derivatives of uploaded files); a verification input as `{type, question, answers}`."""
    if isinstance(value, str):
        return value
    if isinstance(value, VerificationInput):
        return {
            "type": "verification",
            "question": value.question,
            "answers": [{"id": i, "text": t} for i, t in value.answers],
        }
    if isinstance(value, tuple):
        return [_message(m) for m in value]
    return value


def _message(msg: Msg) -> dict[str, object]:
    return {"role": msg.role, "content": [canonical_part(p) for p in msg.parts]}


def canonical_part(part: ContentPart) -> dict[str, object]:
    """One message part as the digest sees it (spec-06 §3.4)."""
    if part.kind == "text":
        return {"type": "input_text", "text": part.text or ""}
    if part.kind == "json":
        return {"type": "input_json", "json": part.json}
    wire_type = "input_image" if part.kind == "image" else "input_file"
    if part.file is None:  # unreachable for resolved parts: media parts always carry their FileRef
        return {"type": wire_type}
    if part.file.origin == "inline":
        return {"type": wire_type, "sha256": part.file.sha256}
    ready = sorted(f"{d.kind}:{d.sha256}" for d in part.file.derivatives if d.status == "ready" and d.sha256)
    return {"type": wire_type, "sha256": part.file.sha256, "derivatives": ready}


def input_view_signature(views: Sequence[tuple[str, str]]) -> str:
    """`sha256(",".join(f"{member_id}:{view_kind}"))` over the stage's members in profile order (spec-06 §3.5)."""
    text = ",".join(f"{member_id}:{kind}" for member_id, kind in views)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def option_order_seed(decision_id: str) -> int:
    """The first 4 bytes of sha256(decision_id), masked to 31 bits (IMPL-051)."""
    return int.from_bytes(hashlib.sha256(decision_id.encode()).digest()[:4], "big") & 0x7FFFFFFF


def permutation(seed: int, profile_ref: str, member_id: str, qname: str, k: int) -> list[int]:
    """Letter j (A+j) shows declared option `p[j]` (spec-06 §3.6). Only choice questions are permuted."""
    digest = hashlib.sha256(f"{seed}|{profile_ref}|{member_id}|{qname}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 (a reproducible shuffle, not security)
    order = list(range(k))
    rng.shuffle(order)
    return order


def _plain(obj: object) -> object:
    """json default hook: read-only mappings and tuples as plain JSON containers."""
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, tuple | frozenset):
        return list(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")
