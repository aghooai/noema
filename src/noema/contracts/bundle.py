"""The bundled OpenAPI document: loading, `$ref` resolution, JSON Schema validators and the operation table.

Validators run on jsonschema-rs (Draft 2020-12, formats checked, offline). The whole bundle is registered as one
resource under `BUNDLE_URI`, so a validator for any location keeps the document's own `#/components/...` refs.
"""

from __future__ import annotations

import functools
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import jsonschema_rs
import yaml

CONTRACTS_DIR: Final = Path(__file__).resolve().parent
VENDOR_DIR: Final = CONTRACTS_DIR / "_vendor"
BUNDLE_PATH: Final = CONTRACTS_DIR / "build" / "openapi.bundled.yaml"
BUNDLE_URI: Final = "urn:noema:openapi"
_FRAGMENT_SAFE: Final = "/~-._!$&'()*+,;=:@"  # RFC 3986 fragment characters; everything else is %-encoded
HTTP_METHODS: Final = ("get", "put", "post", "delete", "patch", "head", "options")

ParamLocation = Literal["path", "query", "header"]


def pointer(*parts: str | int) -> str:
    """An RFC 6901 JSON pointer from unescaped reference tokens."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@dataclass(frozen=True, slots=True)
class RawParameter:
    """One operation parameter with its `$ref` resolved; `schema_ptr` locates its schema in the bundle."""

    name: str
    location: ParamLocation
    required: bool
    schema: Mapping[str, Any]
    schema_ptr: str


@dataclass(frozen=True, slots=True)
class RawOperation:
    """One operation of the bundle. `body_ptrs` maps each request media type to its schema pointer; `responses`
    maps each status ("200", "4XX", "default") to {media type: schema pointer} (empty for no content)."""

    operation_id: str
    method: str
    path: str
    parameters: tuple[RawParameter, ...]
    body_required: bool
    body_ptrs: Mapping[str, str]
    responses: Mapping[str, Mapping[str, str]]
    response_headers: Mapping[str, tuple[str, ...]]
    authenticated: bool
    error_codes: tuple[str, ...]
    roles: tuple[str, ...]


class Bundle:
    """A loaded bundle. Validators are compiled lazily and cached per pointer."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc: dict[str, Any] = doc
        self._registry = jsonschema_rs.Registry([(BUNDLE_URI, doc)], draft=jsonschema_rs.Draft202012)
        self._validators: dict[str, jsonschema_rs.Validator] = {}

    @classmethod
    def load(cls, path: Path = BUNDLE_PATH) -> Bundle:
        """Parse the bundle YAML (safe loader)."""
        with path.open("rb") as fh:
            doc = yaml.load(fh, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506 (safe loaders only)
        if not isinstance(doc, dict) or "paths" not in doc:
            raise ValueError(f"{path} is not an OpenAPI document")
        return cls(doc)

    def at(self, ptr: str) -> Any:
        """The node at a JSON pointer (no `$ref` following)."""
        node: Any = self.doc
        for token in ptr.split("/")[1:]:
            key = token.replace("~1", "/").replace("~0", "~")
            node = node[int(key)] if isinstance(node, list) else node[key]
        return node

    def resolve(self, node: Any) -> Any:
        """Follow local `$ref`s until a non-reference node."""
        seen = 0
        while isinstance(node, Mapping) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise ValueError(f"unsupported $ref {ref!r}")
            node = self.at(ref[1:])
            seen += 1
            if seen > 64:
                raise ValueError("$ref cycle")
        return node

    def validator(self, ptr: str) -> jsonschema_rs.Validator:
        """A Draft 2020-12 validator for the schema at `ptr` (formats checked, no network)."""
        v = self._validators.get(ptr)
        if v is None:
            v = jsonschema_rs.Draft202012Validator(
                {"$ref": f"{BUNDLE_URI}#{urllib.parse.quote(ptr, safe=_FRAGMENT_SAFE)}"},
                registry=self._registry,
                validate_formats=True,
                offline=True,
            )
            self._validators[ptr] = v
        return v

    def operations(self) -> tuple[RawOperation, ...]:
        """Every operation, in document order."""
        return tuple(self._iter_operations())

    def _ref_ptr(self, node: Any, ptr: str) -> tuple[Any, str]:
        """Follow `$ref`s, returning the target node and its pointer."""
        while isinstance(node, Mapping) and "$ref" in node:
            ptr = str(node["$ref"])[1:]
            node = self.at(ptr)
        return node, ptr

    def _iter_operations(self) -> Iterator[RawOperation]:
        default_security = self.doc.get("security", [])
        for path, item in self.doc["paths"].items():
            shared: Sequence[Any] = item.get("parameters", [])
            for method in HTTP_METHODS:
                op = item.get(method)
                if op is None:
                    continue
                op_ptr = pointer("paths", path, method)
                params: dict[tuple[str, str], RawParameter] = {}
                for base, nodes in ((pointer("paths", path), shared), (op_ptr, op.get("parameters", []))):
                    for i, raw in enumerate(nodes):
                        p, p_ptr = self._ref_ptr(raw, f"{base}/parameters/{i}")
                        loc = p["in"]
                        if loc not in ("path", "query", "header"):
                            raise ValueError(f"{op['operationId']}: unsupported parameter location {loc!r}")
                        name = p["name"].lower() if loc == "header" else p["name"]
                        params[(loc, name)] = RawParameter(
                            name=p["name"],
                            location=loc,
                            required=bool(p.get("required", False)),
                            schema=self.resolve(p["schema"]),
                            schema_ptr=f"{p_ptr}/schema",
                        )
                body_ptrs: dict[str, str] = {}
                body_required = False
                if "requestBody" in op:
                    rb, rb_ptr = self._ref_ptr(op["requestBody"], f"{op_ptr}/requestBody")
                    body_required = bool(rb.get("required", False))
                    for media in rb.get("content", {}):
                        body_ptrs[media] = f"{rb_ptr}/content/{pointer(media)[1:]}/schema"
                responses: dict[str, dict[str, str]] = {}
                headers: dict[str, tuple[str, ...]] = {}
                for status, raw in op.get("responses", {}).items():
                    r, r_ptr = self._ref_ptr(raw, pointer("paths", path, method, "responses", str(status)))
                    responses[str(status)] = {
                        media: f"{r_ptr}/content/{pointer(media)[1:]}/schema"
                        for media, body in r.get("content", {}).items()
                        if "schema" in body
                    }
                    headers[str(status)] = tuple(
                        name.lower() for name, h in r.get("headers", {}).items() if self.resolve(h).get("required")
                    )
                security = op.get("security", default_security)
                yield RawOperation(
                    operation_id=op["operationId"],
                    method=method.upper(),
                    path=path,
                    parameters=tuple(params.values()),
                    body_required=body_required,
                    body_ptrs=body_ptrs,
                    responses=responses,
                    response_headers=headers,
                    authenticated=bool(security),
                    error_codes=tuple(op.get("x-noema-error-codes", ())),
                    roles=tuple(op.get("x-noema-roles", ())),
                )


@functools.cache
def default_bundle() -> Bundle:
    """The committed bundle (`build/openapi.bundled.yaml`), loaded once per process."""
    return Bundle.load(BUNDLE_PATH)
