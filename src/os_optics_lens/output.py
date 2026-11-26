"""Response envelopes, bounded serialization and pagination cursors (spec §7, amendments 10.13-10.15).

Every response is one JSON document. Its wire form is `serialize(response)`: compact JSON with
`ensure_ascii=True` followed by a single `"\\n"`, so its length is the same on every platform and
console encoding. `bounds.output_bytes` is exactly that length, newline included, and it never
exceeds `bounds.max_output_bytes`.

Budgeting:

* `finalize(response, max_output_bytes)` settles `bounds` for a response whose size is fixed and
  raises `OUTPUT_LIMIT_EXCEEDED` (with `details.required_bytes`) when it does not fit.
* `fill_pages(response, slots, max_output_bytes, batch=...)` fills candidate pages: the response
  skeleton (every page empty) is reserved first, then candidates are added round-robin in query order
  while the whole document still fits. A page whose first candidate does not fit gets that candidate
  in its compact form; if even that does not fit, the page fails with `OUTPUT_LIMIT_EXCEEDED` (the
  whole request for a single query, only that entry for a batch). A page therefore never comes back
  with `returned: 0` and a `next_cursor` equal to its own offset.
* `bounded_error_response(error, max_output_bytes)` is the error envelope shrunk to the budget:
  long echoed values are shortened, `BUILD_FAILED` findings are halved into `findings_omitted`,
  and `details.truncated` is true whenever anything was cut.

Cursors are urlsafe base64 (no padding) of canonical JSON `{"v": 2, "r": reader, "s": snapshot_id,
"q": query_digest, "o": offset}`, where `reader` is the first 16 hex of
`semantics.reader_fingerprint()`: an offset means something only under the code and vocabulary that
ordered the results.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .canonical import canonical_json
from .errors import EXIT_CODES, LensError
from .semantics import reader_fingerprint

DEFAULT_MAX_OUTPUT_BYTES = 32768
MIN_MAX_OUTPUT_BYTES = 2048
MAX_MAX_OUTPUT_BYTES = 262144
CURSOR_VERSION = 2
_CURSOR_KEYS = frozenset({"v", "r", "s", "q", "o"})
_RESTART_HINT = "Restart the search without a cursor."

_OUTPUT_HINT = ("Raise max_output_bytes (at most 262144), lower limit, request fewer fields or "
                "queries, or narrow evidence/prescription to named sections.")


# ---------------------------------------------------------------------------------------------
# Serialization


def dumps(obj: Any) -> str:
    """Compact, ASCII-only JSON text (no trailing newline). Non-finite numbers are rejected."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def serialize(response: Any, *, pretty: bool = False) -> bytes:
    """The wire form of a response: ASCII JSON plus `"\\n"`.

    The compact form (the default) is the form `bounds` accounts for. `pretty=True` indents by two
    spaces for human reading; it is longer than the accounted size.
    """
    if pretty:
        text = json.dumps(response, ensure_ascii=True, indent=2, allow_nan=False)
    else:
        text = dumps(response)
    return (text + "\n").encode("ascii")


def wire_length(response: Any) -> int:
    """Length in bytes of `serialize(response)` (compact form)."""
    return len(dumps(response)) + 1


def snapshot_info(index: Any) -> dict[str, Any]:
    """The `snapshot` member of every response for an opened index."""
    return {"id": index.snapshot_id, "records": index.record_count,
            "configurations": index.configuration_count}


def error_response(error: LensError) -> dict[str, Any]:
    """The error envelope `{"ok": false, "error": {code, message, hint, details}}`."""
    return {"ok": False, "error": error.to_dict()}


_CLIP_LIMITS = (1024, 256, 96)
_CLIP_DEPTH = 4


def clip_value(value: Any, limit: int, depth: int = 0) -> Any:
    """`value` with every string longer than `limit` characters shortened (ending in "...") and
    every container whose compact JSON exceeds `limit` either clipped member-wise (objects and
    lists near the top) or replaced by a shortened JSON text. Never longer than the input."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."
    if not isinstance(value, (dict, list)):
        return value
    text = dumps(value)
    if len(text) <= limit:
        return value
    if depth < _CLIP_DEPTH and isinstance(value, dict) and len(value) <= 32:
        return {clip_value(str(key), limit): clip_value(item, limit, depth + 1) for key, item in value.items()}
    if depth < _CLIP_DEPTH and isinstance(value, list) and len(value) <= 32:
        return [clip_value(item, limit, depth + 1) for item in value]
    return text[: max(0, limit - 3)] + "..."


def clip_error(error: dict[str, Any], limit: int) -> dict[str, Any]:
    """An error object (`LensError.to_dict()`) with echoed values clipped to `limit` characters;
    `details.truncated` is set when anything changed. Used for batch error entries."""
    clipped = {key: clip_value(value, limit, 1) for key, value in error.items()}
    if clipped != error:
        details = clipped.get("details")
        clipped["details"] = dict(details) if isinstance(details, dict) else {}
        clipped["details"]["truncated"] = True
    return clipped


def bounded_error_response(error: LensError, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """The error envelope, shrunk until its wire form fits `max_output_bytes` (spec §7: every
    document on stdout is bounded, error envelopes included).

    Shrinking steps, each tried only while the envelope is still too large: clip echoed strings and
    values (1024, then 256, then 96 characters); halve a `BUILD_FAILED` findings list, counting the
    dropped ones in `findings_omitted`; finally keep only code, shortened message and hint, the
    pointer and the findings counts. `details.truncated` is true whenever anything was cut.
    """
    envelope = error_response(error)
    if wire_length(envelope) <= max_output_bytes:
        return envelope
    raw = error.to_dict()
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    findings = details.get("findings") if isinstance(details.get("findings"), list) else None
    omitted = details.get("findings_omitted") if isinstance(details.get("findings_omitted"), int) else 0
    for limit in _CLIP_LIMITS:
        head = {key: clip_value(raw.get(key), limit) for key in ("code", "message", "hint")}
        clipped = {key: clip_value(value, limit, 1) for key, value in details.items() if key != "findings"}
        clipped["truncated"] = True
        if findings is None:
            envelope = {"ok": False, "error": {**head, "details": clipped}}
            if wire_length(envelope) <= max_output_bytes:
                return envelope
            continue
        items = [clip_value(item, limit, 1) for item in findings]
        shown = len(items)
        while True:
            envelope = {"ok": False, "error": {**head, "details": {
                **clipped, "findings": items[:shown], "findings_omitted": omitted + len(items) - shown}}}
            if wire_length(envelope) <= max_output_bytes or shown == 0:
                break
            shown //= 2
        if wire_length(envelope) <= max_output_bytes:
            return envelope
    minimal: dict[str, Any] = {"truncated": True}
    if isinstance(details.get("pointer"), str):
        minimal["pointer"] = clip_value(details["pointer"], 96)
    if findings is not None:
        minimal.update(findings=[], findings_omitted=omitted + len(findings))
    return {"ok": False, "error": {"code": clip_value(raw.get("code"), 64),
                                   "message": clip_value(raw.get("message"), 96),
                                   "hint": clip_value(raw.get("hint"), 96), "details": minimal}}


def exit_code(response: dict[str, Any]) -> int:
    """Process exit code for a response: 0 when `ok`, else the code of its (first) error.

    A batch with at least one successful entry is `ok`; a batch whose entries all failed exits with
    the code of its first entry's error.
    """
    if response.get("ok"):
        return 0
    error = response.get("error")
    if error is None:
        for entry in response.get("results") or []:
            if not entry.get("ok") and entry.get("error"):
                error = entry["error"]
                break
    code = error.get("code") if isinstance(error, dict) else None
    return EXIT_CODES.get(code, 1) if code else 1


def check_max_output_bytes(value: Any, pointer: str) -> int:
    """Validate a requested output budget (never clamped): an integer in [2048, 262144]."""
    if value is None:
        return DEFAULT_MAX_OUTPUT_BYTES
    if isinstance(value, bool) or not isinstance(value, int) or not (
            MIN_MAX_OUTPUT_BYTES <= value <= MAX_MAX_OUTPUT_BYTES):
        raise LensError(
            "INVALID_REQUEST", f"max_output_bytes must be an integer from {MIN_MAX_OUTPUT_BYTES} to "
            f"{MAX_MAX_OUTPUT_BYTES}",
            hint=f"default {DEFAULT_MAX_OUTPUT_BYTES}",
            details={"pointer": pointer, "actual": value,
                     "allowed": {"min": MIN_MAX_OUTPUT_BYTES, "max": MAX_MAX_OUTPUT_BYTES}})
    return value


# ---------------------------------------------------------------------------------------------
# Bounds


def _settle_output_bytes(response: dict[str, Any], base_length: int) -> int:
    """Set `bounds.output_bytes` so it equals the document length.

    `base_length` is the wire length with `output_bytes` written as `0` (one digit); the final value
    is the fixed point of `length = base_length - 1 + digits(length)`.
    """
    total = base_length
    while True:
        candidate = base_length - 1 + len(str(total))
        if candidate == total:
            break
        total = candidate
    response["bounds"]["output_bytes"] = total
    return total


def _output_limit(required: int, budget: int, **details: Any) -> LensError:
    return LensError(
        "OUTPUT_LIMIT_EXCEEDED",
        f"the response needs {required} bytes but max_output_bytes is {budget}",
        hint=_OUTPUT_HINT,
        details={"required_bytes": required, "max_output_bytes": budget, **details},
    )


def finalize(response: dict[str, Any], max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """Attach exact `bounds` to a response of fixed content; raise OUTPUT_LIMIT_EXCEEDED if it
    does not fit in `max_output_bytes`. Returns the same (mutated) response."""
    response["bounds"] = {"max_output_bytes": max_output_bytes, "output_bytes": 0, "truncated": False}
    total = _settle_output_bytes(response, wire_length(response))
    if total > max_output_bytes:
        raise _output_limit(total, max_output_bytes)
    return response


@dataclass
class PageSlot:
    """One candidate page to fill inside a response.

    `page_holder` is the result object owning `candidates` and `page`; `full` and `compact` are the
    page's candidates in order (same length; compact drops summary facts and fact details).
    `offset` is the absolute offset of `full[0]`, `listed` the length of the full ordered list, and
    `cursor_at(offset)` encodes the continuation cursor for an absolute offset. `entry_index` is the
    position of the owning entry in a batch's `results` (None for a single query).
    """

    page_holder: dict[str, Any]
    full: list[dict[str, Any]]
    compact: list[dict[str, Any]]
    offset: int
    listed: int
    cursor_at: Callable[[int], str]
    entry_index: int | None = None
    chosen: list[int] = field(default_factory=list)
    """Serialized sizes of the candidates chosen so far."""
    compacted: bool = False
    closed: bool = False

    def next_cursor(self, returned: int) -> str | None:
        """The continuation cursor after `returned` candidates of this page, or None at the end."""
        position = self.offset + returned
        return self.cursor_at(position) if position < self.listed else None


def _apply_scalars(response: dict[str, Any], slots: list[PageSlot], budget: int) -> None:
    truncated = False
    for slot in slots:
        returned = len(slot.chosen)
        slot.page_holder["candidates"] = []
        page = slot.page_holder["page"]
        page["returned"] = returned
        page["next_cursor"] = slot.next_cursor(returned)
        if returned < len(slot.full) or slot.compacted:
            truncated = True
    response["bounds"] = {"max_output_bytes": budget, "output_bytes": 0, "truncated": truncated}


def _measure(response: dict[str, Any], slots: list[PageSlot], budget: int) -> int:
    """Exact wire length of the response if the slots' current choices were final."""
    _apply_scalars(response, slots, budget)
    total = wire_length(response)
    for slot in slots:
        sizes = slot.chosen
        if sizes:
            total += sum(sizes) + len(sizes) - 1
    return _settle_output_bytes(response, total)


def _measure_alone(response: dict[str, Any], slots: list[PageSlot], slot: PageSlot, budget: int) -> int:
    """Wire length if `slot` kept its reservation and every other page were empty."""
    saved = [(other, other.chosen) for other in slots if other is not slot]
    for other, _ in saved:
        other.chosen = []
    try:
        return _measure(response, slots, budget)
    finally:
        for other, chosen in saved:
            other.chosen = chosen


def fill_pages(response: dict[str, Any], slots: list[PageSlot], max_output_bytes: int, *,
               batch: bool = False,
               error_entry: Callable[[int, LensError], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fill candidate pages within the byte budget (amendment 10.14); returns the response.

    The skeleton (every page empty, continuation cursors at each page's own offset) is reserved
    first; if it alone exceeds the budget the request fails with OUTPUT_LIMIT_EXCEEDED. Next, every
    non-empty page reserves its first candidate in compact form (the progress guarantee), so an
    early page can never starve a later one. A page whose compact first candidate does not fit
    fails: the whole request for a single query, or (with `batch=True`) only that entry, replaced by
    `error_entry(entry_index, error)` in `response["results"]`. Pages that cannot fit even with
    every other page empty fail first, whatever their position (their `required_bytes` is what they
    need alone); only when every remaining page fits alone but they do not fit together do later
    pages give way first.
    Candidates are then added round-robin in slot order: in the first round each page upgrades its
    reserved first candidate to the full form when that fits (else it stays compact and the page
    stops); in later rounds a candidate that does not fit stops its page there (its cursor points
    at that candidate and `bounds.truncated` is true) while other pages keep filling.
    """
    active = list(slots)
    required = _measure(response, active, max_output_bytes)
    if required > max_output_bytes:
        raise _output_limit(required, max_output_bytes, reason="the response skeleton alone does not fit")

    for slot in active:
        if slot.full:
            slot.chosen.append(len(dumps(slot.compact[0])))
            slot.compacted = True
    while True:
        needed = _measure(response, active, max_output_bytes)
        if needed <= max_output_bytes:
            break
        failing = [slot for slot in active if slot.chosen]
        victim = None
        for slot in failing:
            alone = _measure_alone(response, active, slot, max_output_bytes)
            if alone > max_output_bytes:
                victim, needed = slot, alone
                break
        if victim is None and failing:
            victim = failing[-1]
        error = _output_limit(needed, max_output_bytes,
                              reason="the first candidate of a page does not fit even in compact form")
        if victim is None or not batch or error_entry is None or victim.entry_index is None:
            raise error
        error.details["pointer"] = f"/queries/{victim.entry_index}"
        response["results"][victim.entry_index] = error_entry(victim.entry_index, error)
        active.remove(victim)

    progressing = True
    while progressing:
        progressing = False
        for slot in active:
            if slot.closed or not slot.full:
                continue
            if slot.compacted:
                reserved = slot.chosen[0]
                slot.chosen[0] = len(dumps(slot.full[0]))
                if _measure(response, active, max_output_bytes) <= max_output_bytes:
                    slot.compacted = False
                    progressing = True
                else:
                    slot.chosen[0] = reserved
                    slot.closed = True
                continue
            if len(slot.chosen) >= len(slot.full):
                continue
            slot.chosen.append(len(dumps(slot.full[len(slot.chosen)])))
            if _measure(response, active, max_output_bytes) <= max_output_bytes:
                progressing = True
                continue
            slot.chosen.pop()
            slot.closed = True
    _apply_scalars(response, active, max_output_bytes)
    for slot in active:
        chosen = [slot.compact[0]] if slot.compacted else slot.full[: len(slot.chosen)]
        slot.page_holder["candidates"] = chosen
    total = _settle_output_bytes(response, wire_length(response))
    if total > max_output_bytes:  # pragma: no cover - guarded by the exact accounting above
        raise LensError("INTERNAL", "output accounting error",
                        details={"required_bytes": total, "max_output_bytes": max_output_bytes})
    return response


# ---------------------------------------------------------------------------------------------
# Cursors


def query_digest(normalized_query: dict[str, Any]) -> str:
    """First 16 hex of SHA-256 over the canonical JSON of a normalized query (amendment 10.15)."""
    return hashlib.sha256(canonical_json(normalized_query).encode("utf-8")).hexdigest()[:16]


def cursor_reader() -> str:
    """The reader fingerprint as cursors carry it."""
    return reader_fingerprint()[:16]


def encode_cursor(snapshot_id: str, digest: str, offset: int) -> str:
    """The continuation cursor for an absolute offset into a query's ordered result list."""
    payload = canonical_json({"v": CURSOR_VERSION, "r": cursor_reader(), "s": snapshot_id, "q": digest,
                              "o": offset})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: Any, snapshot_id: str, digest: str, pointer: str = "/cursor") -> int:
    """The absolute offset a cursor encodes, after checking it belongs to this reader, snapshot
    and query.

    Raises CURSOR_INVALID (undecodable, or a version 1 cursor), CURSOR_SEMANTICS_MISMATCH (made by
    a reader with other query code or vocabulary), SNAPSHOT_MISMATCH (made on another index
    snapshot) or CURSOR_MISMATCH (made for a different query).
    """
    def invalid(reason: str, hint: str = "Pass next_cursor exactly as returned, with the same query.") -> LensError:
        return LensError("CURSOR_INVALID", f"the cursor cannot be decoded: {reason}", hint=hint,
                         details={"pointer": pointer})

    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise invalid("expected the next_cursor string")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise invalid("not a cursor") from None
    if isinstance(data, dict) and set(data) == _CURSOR_KEYS - {"r"} and data["v"] == 1:
        raise invalid("it is a version 1 cursor, which does not record the reader that ordered the results",
                      _RESTART_HINT)
    if not isinstance(data, dict) or set(data) != _CURSOR_KEYS or data["v"] != CURSOR_VERSION:
        raise invalid("unknown cursor format")
    offset = data["o"]
    if not all(isinstance(data[key], str) for key in ("r", "s", "q")) or isinstance(offset, bool) \
            or not isinstance(offset, int) or offset < 0:
        raise invalid("malformed cursor fields")
    if data["r"] != cursor_reader():
        raise LensError(
            "CURSOR_SEMANTICS_MISMATCH",
            "the cursor was made by a reader with different query code or vocabulary, so its offset "
            "does not continue the same ordered results",
            hint=_RESTART_HINT,
            details={"pointer": pointer, "cursor_reader": data["r"], "reader": cursor_reader()})
    if data["s"] != snapshot_id:
        raise LensError(
            "SNAPSHOT_MISMATCH",
            f"the cursor was made on index snapshot {data['s']}, this index is {snapshot_id}",
            hint="Rerun the query without a cursor; results may differ on the new snapshot.",
            details={"pointer": pointer, "cursor_snapshot": data["s"], "snapshot": snapshot_id})
    if data["q"] != digest:
        raise LensError(
            "CURSOR_MISMATCH", "the cursor belongs to a different query",
            hint="Send the same mode, filters, constraints, targets, preferences and include with the cursor.",
            details={"pointer": pointer, "cursor_query": data["q"], "query": digest})
    return offset
