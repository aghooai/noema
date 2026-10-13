"""Operation lookup: (method, path) -> the bundled openapi operation (IMPL-064).

The table is built from the same bundle as the request gate, so the gate, the routes and the contract agree on
every path template. An unknown path, or a known path with an undeclared method (Starlette's 405), is 404
`not_found`. Path parameters match one non-empty segment; trailing slashes are not normalized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from noema.contracts.bundle import RawOperation

_PARAM: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class RouteMatch:
    """A matched operation and its raw (already percent-decoded) path parameters."""

    operation: RawOperation
    path_params: dict[str, str]


def _compile(template: str) -> re.Pattern[str]:
    out, pos = [], 0
    for m in _PARAM.finditer(template):
        out.append(re.escape(template[pos : m.start()]))
        out.append(f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("".join(out))


class OperationTable:
    """Matches requests against the operations of one bundle. Literal paths are tried first, then the templated
    ones in document order."""

    def __init__(self, operations: tuple[RawOperation, ...]) -> None:
        self._literal: dict[tuple[str, str], RawOperation] = {}
        self._templated: list[tuple[re.Pattern[str], RawOperation]] = []
        for op in operations:
            if _PARAM.search(op.path):
                self._templated.append((_compile(op.path), op))
            else:
                self._literal[(op.method, op.path)] = op
        self.operations: Final = operations

    def match(self, method: str, path: str) -> RouteMatch | None:
        """The operation for `method` (upper-case) and the decoded `path`, or None."""
        op = self._literal.get((method, path))
        if op is not None:
            return RouteMatch(op, {})
        for pattern, candidate in self._templated:
            if candidate.method != method:
                continue
            m = pattern.fullmatch(path)
            if m is not None:
                return RouteMatch(candidate, m.groupdict())
        return None
