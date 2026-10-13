"""Request parameters as TypedDicts. The SDK does not validate them: the server's request gate is authoritative."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict


class InputText(TypedDict):
    type: Literal["input_text"]
    text: str


class InputImageUrl(TypedDict):
    type: Literal["input_image"]
    image_url: str  # data URL; inline images become ephemeral files and go through the sandbox
    detail: NotRequired[Literal["low", "high", "auto", "original"]]


class InputImageFile(TypedDict):
    type: Literal["input_image"]
    file_id: str


class InputFile(TypedDict):
    type: Literal["input_file"]
    file_id: str


class InputJson(TypedDict):
    type: Literal["input_json"]
    json: Any


InputPart = InputText | InputImageUrl | InputImageFile | InputFile | InputJson


class UserMessage(TypedDict):
    role: Literal["user"]
    content: list[InputPart]


DecisionInput = str | list[UserMessage] | dict[str, Any]


class Criteria(TypedDict, total=False):
    true: str
    false: str


class PredicateQuestionParam(TypedDict):
    type: Literal["predicate", "noul"]
    name: str
    instructions: str
    criteria: NotRequired[Criteria]


class ChoiceOptionParam(TypedDict):
    value: str
    description: NotRequired[str | None]
    group: NotRequired[str]


class ChoiceQuestionParam(TypedDict):
    type: Literal["choice"]
    name: str
    instructions: str
    choices: list[ChoiceOptionParam]


class ScoreLevelParam(TypedDict):
    label: str
    description: NotRequired[str | None]


class ScoreQuestionParam(TypedDict):
    type: Literal["score"]
    name: str
    instructions: str
    levels: list[ScoreLevelParam]


QuestionParam = PredicateQuestionParam | ChoiceQuestionParam | ScoreQuestionParam


class DecisionParams(TypedDict, total=False):
    """`POST /v1/decisions` fields other than `questions` and `output_format`."""

    input: Required[DecisionInput]
    profile: str  # "router-default" or "router-default@1"; default: the tenant's defaults.decision_profile
    deadline_ms: int
    min_quorum: int
    escalation: Literal["profile_default", "off"]
    session_id: str
    include: list[Literal["members", "member_distributions", "stages"]]
    safety_identifier: str


class TurnDecisions(TypedDict, total=False):
    profile: str
    questions: list[QuestionParam]
    blocking: list[str]
    deadline_ms: int
    min_quorum: int
    include: list[Literal["members", "member_distributions", "stages"]]


class TurnGeneration(TypedDict, total=False):
    profile: str  # "auto" or a generation profile id from the allowlist
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    max_output_tokens: int
    temperature: float


class TurnHistory(TypedDict, total=False):
    mode: Required[Literal["auto", "none", "last_n"]]
    max_tokens: int
    last_n: int


class TurnParams(TypedDict, total=False):
    """`POST /v1/turns` fields other than `stream` (set by `turns.stream` / `turns.create`)."""

    input: Required[list[UserMessage]]
    session_id: str
    expected_seq: int
    store: bool
    wait_for_files: bool
    allow_partial_files: bool
    retry_of: str
    instructions: str
    decisions: TurnDecisions
    generation: TurnGeneration
    strategy: Literal["auto", "gate", "holdback", "speculative", "hedge"]
    conditioning: Literal["auto", "off", "direct", "cover_top2_and_clarify", "clarify_first"]
    verify: bool
    history: TurnHistory
    metadata: dict[str, str]


class SessionCreateParams(TypedDict, total=False):
    metadata: dict[str, str]
    retention: Literal["default", "ephemeral", "custom"]
    data_class: Literal["public", "internal", "confidential", "restricted"]


class SessionUpdateParams(TypedDict, total=False):
    """PATCH semantics: present fields replace the stored value; `metadata` replaces the whole map."""

    metadata: dict[str, str]
    retention: Literal["default", "ephemeral", "custom"]
    data_class: Literal["public", "internal", "confidential", "restricted"]


class RequestOptions(TypedDict, total=False):
    """Per-call overrides. `timeout` bounds each httpx operation, so on a stream it bounds every read, not only the
    first event: keep it above the longest silence between events (the server pings every 15 s); a longer gap ends
    the stream with AghooConnectionError. (TS `timeoutMs` stops at the first event.)"""

    idempotency_key: str  # POSTs only; default: one UUIDv4 per call, reused across its retries
    timeout: float  # seconds; httpx's per-operation timeout (connect, write, pool, each read); streams: every read
    max_retries: int
