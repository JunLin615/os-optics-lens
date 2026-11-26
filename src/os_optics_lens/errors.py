"""Error and finding types shared by the builder, the store and the query layer.

`LensError` is the single exception type surfaced to callers and the CLI. Its `code` is a stable
machine-readable identifier; `exit_code` follows the table in `EXIT_CODES`. `Finding` describes one
problem found while validating the corpus; a build with at least one error finding fails with
`BUILD_FAILED`.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

EXIT_CODES: dict[str, int] = {
    "INTERNAL": 1,
    "INVALID_REQUEST": 2,
    "UNKNOWN_FACET": 2,
    "UNKNOWN_FACET_VALUE": 2,
    "AMBIGUOUS_TERM": 2,
    "UNKNOWN_FIELD": 2,
    "UNSUPPORTED_OPERATOR": 2,
    "CURSOR_INVALID": 2,
    "CURSOR_MISMATCH": 2,
    "OUTPUT_LIMIT_EXCEEDED": 2,
    "FILE_EXISTS": 2,
    "EVIDENCE_AMBIGUOUS": 2,
    "RECORD_NOT_FOUND": 3,
    "REVISION_NOT_AVAILABLE": 3,
    "CONFIGURATION_NOT_FOUND": 3,
    "EVIDENCE_SECTION_NOT_FOUND": 3,
    "SNAPSHOT_MISMATCH": 4,
    "CURSOR_SEMANTICS_MISMATCH": 4,
    "INDEX_UNAVAILABLE": 5,
    "INDEX_IN_USE": 5,
    "BUILD_FAILED": 6,
}
"""Process exit code for each error code. Unknown codes exit with 1."""


class LensError(Exception):
    """A user-facing failure with a stable code, an English message and an optional hint.

    `details` holds machine-readable context (for example `findings` for `BUILD_FAILED`, `tried` for
    `INDEX_UNAVAILABLE`, `did_you_mean` for lookups). `exit_code` defaults to `EXIT_CODES[code]`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details if details is not None else {}
        self.exit_code = exit_code if exit_code is not None else EXIT_CODES.get(code, 1)
        self.output_budget: int | None = None
        """The byte budget of the request that failed, when known; the CLI bounds the error
        envelope by it (else by --max-output-bytes or the default)."""

    def to_dict(self) -> dict[str, Any]:
        """The `error` member of the response envelope."""
        return {"code": self.code, "message": self.message, "hint": self.hint, "details": self.details}

    def __repr__(self) -> str:
        return f"LensError({self.code!r}, {self.message!r})"


ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One corpus problem.

    `file` is a corpus-relative POSIX path; `field` is a dotted path with `[i]` list indices into the
    file's JSON (empty when the problem concerns the whole file). `expected`/`actual` are reported
    only when `has_expected` is true (see `finding(..., expected=..., actual=...)`).
    """

    severity: str
    code: str
    record_id: str | None
    file: str
    field: str
    message: str
    expected: Any = dataclass_field(default=None, compare=False)
    actual: Any = dataclass_field(default=None, compare=False)
    has_expected: bool = dataclass_field(default=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """The finding as a JSON object; `expected`/`actual` appear only when reported."""
        out: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "record_id": self.record_id,
            "file": self.file,
            "field": self.field,
            "message": self.message,
        }
        if self.has_expected:
            out["expected"] = self.expected
            out["actual"] = self.actual
        return out

    def sort_key(self) -> tuple[str, str, str, str, str]:
        """Findings are ordered by (file, field, code), then record id and message for stability."""
        return (self.file, self.field, self.code, self.record_id or "", self.message)

    def as_warning(self) -> "Finding":
        """The same finding downgraded to a warning."""
        return Finding(WARNING, self.code, self.record_id, self.file, self.field, self.message,
                       self.expected, self.actual, self.has_expected)


def finding(
    severity: str,
    code: str,
    record_id: str | None,
    file: str,
    field_path: str,
    message: str,
    **compare: Any,
) -> Finding:
    """Build a `Finding`; pass `expected=` and `actual=` together to report both values."""
    if compare:
        unknown = set(compare) - {"expected", "actual"}
        if unknown:
            raise TypeError(f"unexpected keyword arguments: {sorted(unknown)}")
        return Finding(severity, code, record_id, file, field_path, message,
                       compare.get("expected"), compare.get("actual"), True)
    return Finding(severity, code, record_id, file, field_path, message)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Findings in their documented order, duplicates removed."""
    unique: dict[tuple[str, str, str, str, str, str], Finding] = {}
    for item in findings:
        unique.setdefault((item.severity, *item.sort_key()), item)
    return sorted(unique.values(), key=lambda f: (*f.sort_key(), f.severity))
