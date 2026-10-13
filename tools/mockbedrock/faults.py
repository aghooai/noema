"""Wire errors of the scenario faults (spec-10 §2.1a "Errors before the body", §2.4 `fault.kind`).

Converse/ConverseStream errors are `HTTP status + x-amzn-ErrorType: <Name> + {"message"}`. Mantle errors are
`HTTP status + {"error": {"message", "type", "code", "param"}}`. Messages are the standard Bedrock texts where
known; the probes captured none of these, so they are documented as mock strings (docs/modules/qa-tools.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tests.fakes.scenario import Fault

THROTTLED: Final = "Too many requests, please wait before trying again."
TOKENS_THROTTLED: Final = "Too many tokens, please wait before trying again."
ACCESS_DENIED: Final = "You don't have access to the model with the specified model ID."
BAD_TOKEN: Final = "The security token included in the request is invalid."  # noqa: S105 (an error message)
VALIDATION_DEFAULT: Final = "Malformed input request, please reformat your input and try again."
INTERNAL: Final = "The system encountered an unexpected error during processing. Try your request again."
UNAVAILABLE: Final = "Bedrock is unable to process your request."
MODEL_TIMEOUT: Final = "Model has timed out in processing the request. Try your request again."
STREAM_ERROR: Final = "The model encountered an error while streaming the response. Try your request again."
NOT_FOUND: Final = "The requested resource could not be found."
NOT_READY: Final = "The model is not ready to serve inference requests. Try your request again later."
QUOTA_EXCEEDED: Final = "Your request exceeds the service quota for your account. Try your request again later."
MODEL_ERROR: Final = "The request failed because the model returned an error."
REFUSAL_TEXT: Final = "I'm sorry, but I can't help with that."


@dataclass(frozen=True, slots=True)
class WireError:
    """One error answer, renderable on both wires."""

    status: int
    name: str  # x-amzn-ErrorType (Converse)
    message: str
    mantle_type: str
    mantle_code: str | None = None
    retry_after_s: float | None = None


def throttled(message: str = THROTTLED, retry_after_s: float | None = None) -> WireError:
    return WireError(429, "ThrottlingException", message, "rate_limit_error", "rate_limit_exceeded", retry_after_s)


def validation(message: str) -> WireError:
    return WireError(400, "ValidationException", message, "invalid_request_error", "validation_error")


def internal(message: str = INTERNAL) -> WireError:
    return WireError(500, "InternalServerException", message, "server_error")


def unrecognized_client() -> WireError:
    return WireError(403, "UnrecognizedClientException", BAD_TOKEN, "authentication_error", "invalid_api_key")


SIGNATURE_MISMATCH: Final = (
    "The request signature we calculated does not match the signature you provided. Check your AWS Secret Access "
    "Key and signing method. Consult the service documentation for details."
)


def signature_mismatch() -> WireError:
    """The 403 for a request whose path is not the one signed (the mock cannot verify signatures; it rejects the
    one mismatch it can see: a raw ':' in the model label, which botocore always percent-encodes)."""
    return WireError(403, "InvalidSignatureException", SIGNATURE_MISMATCH, "authentication_error", "invalid_signature")


def bad_credential_scope(region: str | None, service: str | None, expected_region: str) -> WireError | None:
    """The 403 for a SigV4 credential scoped to another service than `bedrock` or another region than the mock's."""
    if service != "bedrock":
        message = "Credential should be scoped to correct service: 'bedrock'."
    elif region != expected_region:
        message = f"Credential should be scoped to a valid region, not '{region}'."
    else:
        return None
    return WireError(403, "InvalidSignatureException", message, "authentication_error", "invalid_signature")


def pre_byte_error(fault: Fault) -> WireError | None:
    """The HTTP error of a pre-byte fault; None for `connect_reset` (a TCP RST, no response at all)."""
    kind = fault.kind
    if kind == "throttling":
        return throttled(retry_after_s=fault.retry_after_s)
    if kind == "access_denied":
        return WireError(
            403, "AccessDeniedException", fault.message or ACCESS_DENIED, "permission_error", "access_denied"
        )
    if kind == "validation":
        return validation(fault.message or VALIDATION_DEFAULT)
    if kind == "http_5xx":
        if fault.status == 503:
            return WireError(503, "ServiceUnavailableException", fault.message or UNAVAILABLE, "server_error")
        return internal(fault.message or INTERNAL)
    if kind == "model_timeout":
        return WireError(408, "ModelTimeoutException", fault.message or MODEL_TIMEOUT, "timeout_error", "timeout")
    if kind == "connect_reset":
        return None
    raise ValueError(f"{kind} is not a pre-byte fault")


# Exception names (lowerCamel as in `:exception-type`) -> (HTTP status, x-amzn-ErrorType, default message). The first
# five are the ConverseStream exception events; the statuses are the bedrock-runtime service model's (botocore), so
# `exception_event{type}` on a non-streaming call also produces the errors that no fault kind has (spec-10 §2.1a).
_EXCEPTIONS: Final[dict[str, tuple[int, str, str]]] = {
    "modelStreamErrorException": (424, "ModelStreamErrorException", STREAM_ERROR),
    "throttlingException": (429, "ThrottlingException", THROTTLED),
    "internalServerException": (500, "InternalServerException", INTERNAL),
    "serviceUnavailableException": (503, "ServiceUnavailableException", UNAVAILABLE),
    "validationException": (400, "ValidationException", VALIDATION_DEFAULT),
    "accessDeniedException": (403, "AccessDeniedException", ACCESS_DENIED),
    "resourceNotFoundException": (404, "ResourceNotFoundException", NOT_FOUND),
    "modelTimeoutException": (408, "ModelTimeoutException", MODEL_TIMEOUT),
    "modelNotReadyException": (429, "ModelNotReadyException", NOT_READY),
    "serviceQuotaExceededException": (400, "ServiceQuotaExceededException", QUOTA_EXCEEDED),
    "modelErrorException": (424, "ModelErrorException", MODEL_ERROR),
}
_MANTLE_TYPES: Final = {
    400: "invalid_request_error",
    403: "permission_error",
    404: "not_found_error",
    408: "timeout_error",
    429: "rate_limit_error",
}


def exception_payload(exception_type: str, message: str | None) -> dict[str, object]:
    """The JSON payload of a ConverseStream exception frame."""
    text = message or _EXCEPTIONS.get(exception_type, (0, "", INTERNAL))[2]
    if exception_type == "modelStreamErrorException":
        return {"message": text, "originalStatusCode": 500, "originalMessage": "Internal server error"}
    return {"message": text}


def exception_as_http(exception_type: str, message: str | None) -> WireError:
    """`exception_event` on a non-streaming call: the HTTP error of the same exception (no event stream exists)."""
    status, name, default = _EXCEPTIONS.get(
        exception_type, (500, exception_type[:1].upper() + exception_type[1:], INTERNAL)
    )
    return WireError(status, name, message or default, _MANTLE_TYPES.get(status, "server_error"))
