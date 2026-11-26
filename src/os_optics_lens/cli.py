"""Command-line interface `os-optics-lens` (spec §7; amendments 10.13, 10.16, 10.17).

Every invocation writes exactly one JSON document to standard output and exits with the code of
`errors.EXIT_CODES` (0 on success). The document is ASCII JSON (`ensure_ascii`) written as bytes
to `sys.stdout.buffer` followed by one newline, so the output is identical under every console
encoding and its length is what `bounds.output_bytes` reports. Three outputs are not JSON:
`--help` (argparse text), `help <topic>` (the shipped Markdown) and `project --stdout` (the
Project's raw bytes).

Failures are JSON error envelopes `{"ok": false, "error": {code, message, hint, details}}`:

* command-line usage errors (unknown option, bad integer, missing argument) are
  `INVALID_REQUEST` with `details.argv_error` and `details.usage`;
* `LensError`s from the query layer, the store and the builder are passed through unchanged
  (except that `BUILD_FAILED` lists at most `MAX_LISTED_FINDINGS` findings, errors first);
* any other exception is `INTERNAL`; its traceback goes to stderr only when the environment
  variable `OS_OPTICS_LENS_DEBUG=1` is set, never to stdout;
* an error envelope fits the byte budget like any other document (the request's
  `max_output_bytes`, else `--max-output-bytes`, else 32768): echoed values are shortened and
  findings dropped into `findings_omitted`, with `details.truncated: true`.

A reader that closes stdout early (`| head`) ends the process silently with exit code 1
(BrokenPipeError, or OSError EINVAL on a Windows pipe); nothing is written to stderr.

Request documents (`search --request FILE`, `search -`, `search --fixture FILE`) are read as bytes
and decoded by byte-order-mark sniffing: UTF-8 with or without BOM, UTF-16 LE/BE and UTF-32 LE/BE
with BOM.
"""

from __future__ import annotations

import argparse
import difflib
import errno
import hashlib
import os
import re
import sys
import traceback
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__, output, query
from .build import build_index, dump_index, validate_corpus
from .canonical import JsonFormatError, is_number, loads_strict
from .errors import LensError
from .store import ENV_VAR, open_index

PROG = "os-optics-lens"
DEBUG_ENV = "OS_OPTICS_LENS_DEBUG"
DEFAULT_CORPUS = "."
DEFAULT_OUT = "build/os-optics-lens-index.sqlite"
MAX_LISTED_FINDINGS = 50

HELP_TOPICS: dict[str, str] = {
    "quickstart": "From a design brief to an editable Project: facets, fields, search, compare, inspect, project.",
    "request": "The search request format: filters, constraints, targets, preferences, batches, cursors.",
    "response": "The response envelope, search results, bounds and pagination.",
    "semantics": "MATCH / UNCONFIRMED / DOES_NOT_MATCH, reason codes, stop gates, spectral coverage, configurations.",
    "errors": "Error codes, exit codes and what to do about each.",
    "facets": "The discovery facets and their vocabulary.",
    "fields": "Typed fact keys, units, bases and conditions; writing constraints from `fields --fact`.",
}

_QUOTE_STRIPPED = re.compile(r"^\s*\{\s*[A-Za-z_]")
_WHERE_SPLIT = re.compile(r"\s+")
_DIRECTIONS = {"asc": "ascending", "ascending": "ascending", "desc": "descending", "descending": "descending"}
_SOURCE_OPS = ("has_source", "has_sourced_method")


# ---------------------------------------------------------------------------------------------
# argparse plumbing


class UsageError(Exception):
    """A command-line usage error raised by `JsonArgumentParser.error` (reported as INVALID_REQUEST)."""

    def __init__(self, message: str, usage: str) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage


class JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors raise `UsageError` instead of printing text and exiting 2.

    `--help` keeps argparse's text output; it is the only argparse output that is not JSON.
    """

    def error(self, message: str) -> Any:  # type: ignore[override]
        raise UsageError(message, " ".join(self.format_usage().split()))


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the line breaks of descriptions and examples."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=34, width=100)


def _common_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the command (`--index`, `--pretty`)."""
    common = JsonArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--index", metavar="FILE", default=argparse.SUPPRESS,
                        help=f"index file to read (else ${ENV_VAR}, else the bundled index)")
    common.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                        help="indent the JSON output (bounds count the compact form)")
    return common


def _budget_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-output-bytes", type=int, metavar="N",
                        help="output budget in bytes, 2048..262144 (default 32768)")


def _filter_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("discovery filters")
    group.add_argument("--filter", action="append", metavar="FACET=VALUE[,VALUE]",
                       help="facet filter; values OR within a facet (also when repeated), facets AND across")
    group.add_argument("--range", action="append", metavar="FACT=MIN:MAX",
                       help="numeric discovery range over a fact; either end may be empty (FACT=15: or FACT=:30)")
    group.add_argument("--spectral-span", metavar="LOW:HIGH",
                       help="sampling span (nm) overlaps LOW..HIGH; discovery only, never a constraint")
    group.add_argument("--record-id", action="append", metavar="ID", help="restrict to these record ids")


def _add_command(subparsers: Any, common: argparse.ArgumentParser, name: str, summary: str,
                 description: str, examples: Sequence[str]) -> argparse.ArgumentParser:
    epilog = "examples:\n" + "\n".join(f"  {line}" for line in examples)
    return subparsers.add_parser(name, help=summary, description=f"{summary}\n\n{description}".rstrip(),
                                 epilog=epilog, parents=[common], formatter_class=_Formatter,
                                 allow_abbrev=False)


def build_parser() -> JsonArgumentParser:
    """The complete argument parser (every command and subcommand has a description and examples)."""
    common = _common_options()
    parser = JsonArgumentParser(
        prog=PROG, parents=[common], formatter_class=_Formatter, allow_abbrev=False,
        description=(
            "Search a bundled, indexed collection of optical prescriptions and hand off native os-optics\n"
            "Projects. Every command prints one JSON document; failures are {\"ok\": false, \"error\": ...}\n"
            "with a non-zero exit code."),
        epilog=(
            "Start here:  os-optics-lens help quickstart\n"
            "Help topics: os-optics-lens help [" + "|".join(HELP_TOPICS) + "]\n"
            "Command help: os-optics-lens <command> --help"),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", title="commands",
                                       parser_class=JsonArgumentParser)
    subparsers.required = True

    p = _add_command(
        subparsers, common, "info", "Show the index in use: snapshot, counts, source and build metadata.",
        "Use it to confirm which index answers your queries (snapshot ids appear in every response).",
        [f"{PROG} info", f"{PROG} --index build/os-optics-lens-index.sqlite info"])
    _budget_option(p)
    p.set_defaults(handler=_cmd_info)

    p = _add_command(
        subparsers, common, "facets", "List facet values with record counts within the filtered scope.",
        "Start discovery here: values are canonical terms; aliases (e.g. smartphone, ff, nir) are accepted\n"
        "in --filter. Counts are distinct records.",
        [f"{PROG} facets",
         f"{PROG} facets --facet design_type --facet band",
         f"{PROG} facets --filter design_type=camera_lens --facet image_format --describe"])
    p.add_argument("--facet", action="append", metavar="NAME", help="facet to list (repeatable or comma list)")
    p.add_argument("--limit", type=int, metavar="N", help="values per facet, 1..200 (default 20)")
    p.add_argument("--describe", action="store_true", help="add each facet's description")
    _filter_options(p)
    _budget_option(p)
    p.set_defaults(handler=_cmd_facets)

    p = _add_command(
        subparsers, common, "fields", "List typed fact keys within the filtered scope, or describe one key.",
        "Plain form: per fact key the record count, unit, value type, bases, condition variants and range.\n"
        "--fact KEY: applicable operators, condition variants and a ready-to-run example constraint.",
        [f"{PROG} fields --filter design_type=camera_lens",
         f"{PROG} fields --prefix aperture.",
         f"{PROG} fields --fact performance.mtf_field_minimum"])
    p.add_argument("--fact", metavar="KEY", help="describe one fact key for writing a constraint")
    p.add_argument("--prefix", metavar="TEXT", help="only keys starting with TEXT (e.g. performance.)")
    p.add_argument("--limit", type=int, metavar="N", help="keys to list, 1..500 (default 100)")
    _filter_options(p)
    _budget_option(p)
    p.set_defaults(handler=_cmd_fields)

    p = _add_command(
        subparsers, common, "search", "Run a specification or starting-point search (single query or batch).",
        "Give the request as JSON (positional, --request FILE, or - for stdin), as an acceptance fixture\n"
        "(--fixture FILE), or build a single query from quick flags. On Windows PowerShell prefer\n"
        "--request FILE: the shell strips double quotes from inline JSON. --limit, --cursor, --detail,\n"
        "--fields and --max-output-bytes may be combined with a single-query document. See\n"
        "`os-optics-lens help request`.",
        [f"{PROG} search --request brief.json",
         f"{PROG} search --mode specification --filter design_type=camera_lens "
         "--where 'first_order.effective_focal_length_mm between_inclusive [17.1,18.9]' "
         "--where 'image_format.image_circle_diameter_mm gte 43.27'",
         f"{PROG} search --mode starting_point --filter design_type=camera_lens "
         "--target first_order.effective_focal_length_mm=24~3 --prefer design.powered_element_count:asc",
         f"{PROG} search --fixture acceptance/yutong-2025-mirrorless-problem-2.json",
         f"{PROG} search --request brief.json --cursor <next_cursor>"])
    p.add_argument("request_json", nargs="?", metavar="REQUEST_JSON",
                   help="request JSON text, or - to read it from stdin")
    p.add_argument("--request", metavar="FILE", help="read the request JSON from FILE")
    p.add_argument("--fixture", metavar="FILE", help="search with an acceptance fixture (case JSON)")
    p.add_argument("--explain-request", action="store_true",
                   help="print the normalized request and its query digest instead of searching")
    quick = p.add_argument_group("quick flags (build one query; not combinable with a request document)")
    quick.add_argument("--mode", choices=query.MODES, help="required when building a query from flags")
    quick.add_argument("--where", action="append", metavar="'FACT OP VALUE'",
                       help="hard constraint; VALUE is JSON, else a bare string (repeatable)")
    quick.add_argument("--target", action="append", metavar="FACT=VALUE[~TOL|~PCT%]",
                       help="starting_point target, optional absolute or percent tolerance (repeatable)")
    quick.add_argument("--prefer", action="append", metavar="FACT[:asc|desc]",
                       help="ranking preference, ascending by default (repeatable, in order)")
    quick.add_argument("--include", metavar="OUTCOMES", help="comma list of MATCH,UNCONFIRMED,DOES_NOT_MATCH")
    quick.add_argument("--hard-shortfall", choices=query.HARD_SHORTFALL,
                       help="starting_point: keep (allow) or drop (exclude, default) hard DOES_NOT_MATCH")
    _filter_options(p)
    paging = p.add_argument_group("output (combinable with a single-query document)")
    paging.add_argument("--fields", action="append", metavar="K1,K2", help="facts shown per candidate (<= 12)")
    paging.add_argument("--detail", choices=query.DETAILS, help="compact (default) or full per-constraint evidence")
    paging.add_argument("--limit", type=int, metavar="N", help="candidates per page, 1..50 (default 10)")
    paging.add_argument("--cursor", metavar="CURSOR", help="continue with page.next_cursor from a previous response")
    _budget_option(paging)
    p.set_defaults(handler=_cmd_search)

    p = _add_command(
        subparsers, common, "compare", "Compare up to 10 records or configurations side by side.",
        "REF is record_id or record_id@configuration_id. Rows flag values that are not comparable\n"
        "(different unit, conditions or qualifiers) with differs_by.",
        [f"{PROG} compare lenslibrary-528155 yutong-2025-mirrorless-18-p2b",
         f"{PROG} compare lenslibrary-528155 lenslibrary-1843519 "
         "--fields first_order.effective_focal_length_mm,aperture.f_number"])
    p.add_argument("refs", nargs="+", metavar="REF", help="record_id or record_id@configuration_id")
    p.add_argument("--fields", action="append", metavar="K1,K2", help="fact keys to compare (comma list)")
    _budget_option(p)
    p.set_defaults(handler=_cmd_compare)

    p = _add_command(
        subparsers, common, "inspect", "Inspect a record's configurations and Project facts.",
        "The default summary lists available fact keys and supporting calculations. Select facts, view\n"
        "sections, evidence sections, the prescription or direct provenance with the options below.",
        [f"{PROG} inspect yutong-2025-mirrorless-18-p2b",
         f"{PROG} inspect yutong-2025-mirrorless-18-p2b --fields performance.mtf_field_minimum",
         f"{PROG} inspect yutong-2025-mirrorless-18-p2b --evidence all --max-output-bytes 65536",
         f"{PROG} inspect lenslibrary-528155 --prescription summary",
         f"{PROG} inspect lenslibrary-528155 --provenance"])
    p.add_argument("record_id", metavar="RECORD_ID")
    p.add_argument("--revision", type=int, metavar="N", help="fail unless the index holds this revision")
    p.add_argument("--configuration", metavar="ID",
                   help="native configuration id (default: the first; evidence then covers every configuration)")
    p.add_argument("--fields", action="append", metavar="K1,K2", help="fact keys or prefixes to show in full")
    p.add_argument("--sections", action="append", metavar="S1,S2", help="whole view sections (e.g. spectrum)")
    p.add_argument("--evidence", action="append", metavar="S|PATH#S|all",
                   help="evidence sections by name, path#section, or all")
    p.add_argument("--prescription", choices=query.PRESCRIPTION_LEVELS, help="native Project optical model")
    p.add_argument("--provenance", action="store_true", help="direct source, attribution and license terms")
    _budget_option(p)
    p.set_defaults(handler=_cmd_inspect)

    p = _add_command(
        subparsers, common, "project", "Hand off a record's native os-optics Project.",
        "Without --out/--stdout, show metadata (sha256, size, configurations). --out writes the\n"
        "exact bytes (sha256 == the record's native_project.sha256), creating parent directories and\n"
        "refusing to overwrite unless --force. --stdout writes the raw bytes and nothing else. On Windows\n"
        "PowerShell 5.1 use --out: '>' redirection re-encodes the bytes.",
        [f"{PROG} project yutong-2025-mirrorless-18-p2b",
         f"{PROG} project yutong-2025-mirrorless-18-p2b --out work/p2b.project.json",
         f"{PROG} project lenslibrary-528155 --revision 1 --stdout > lens.json"])
    p.add_argument("record_id", metavar="RECORD_ID")
    p.add_argument("--revision", type=int, metavar="N", help="fail unless the index holds this revision")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--out", metavar="FILE", help="write the Project bytes to FILE")
    target.add_argument("--stdout", action="store_true", help="write the raw Project bytes to stdout")
    p.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    _budget_option(p)
    p.set_defaults(handler=_cmd_project)

    p = _add_command(
        subparsers, common, "index", "Build, validate or dump an index from a corpus checkout.",
        "The corpus directory contains records/ (and LICENSE, licenses/). See docs/INDEX_SCHEMA.md.",
        [f"{PROG} index build --corpus . --out build/os-optics-lens-index.sqlite",
         f"{PROG} index validate --corpus .",
         f"{PROG} index dump --index build/os-optics-lens-index.sqlite"])
    index_commands = p.add_subparsers(dest="index_command", metavar="<subcommand>", title="subcommands",
                                      parser_class=JsonArgumentParser)
    index_commands.required = True
    allow_help = ("index records whose evidence references do not resolve; those references then leave "
                  "constraints UNCONFIRMED (never use it to pass a new batch)")
    b = _add_command(
        index_commands, common, "build", "Build an index file from a corpus (atomic replace).",
        "On any error finding the build fails (BUILD_FAILED, exit 6) and a previous --out file is left\n"
        "byte-identical. Output: {out, snapshot_id, records, configurations, facts, warnings, findings, use}.",
        [f"{PROG} index build",
         f"{PROG} index build --corpus path/to/os-optics-lens --out /tmp/lens.sqlite",
         f"{PROG} --index /tmp/lens.sqlite info"])
    b.add_argument("--corpus", default=DEFAULT_CORPUS, metavar="DIR", help="directory containing records/ (default .)")
    b.add_argument("--out", default=DEFAULT_OUT, metavar="FILE", help=f"index file to write (default {DEFAULT_OUT})")
    b.add_argument("--allow-unresolved-evidence", action="store_true", help=allow_help)
    b.set_defaults(handler=_cmd_index_build)
    v = _add_command(
        index_commands, common, "validate", "Run the full build into a temporary file and discard it.",
        "Reports the same findings and snapshot_id as `index build`, with out null.",
        [f"{PROG} index validate", f"{PROG} index validate --corpus path/to/os-optics-lens"])
    v.add_argument("--corpus", default=DEFAULT_CORPUS, metavar="DIR", help="directory containing records/ (default .)")
    v.add_argument("--allow-unresolved-evidence", action="store_true", help=allow_help)
    v.set_defaults(handler=_cmd_index_validate)
    d = _add_command(
        index_commands, common, "dump", "Print the canonical JSON of every table (reproducibility check).",
        "Identical across environments for the same corpus and builder version. The output is not\n"
        "budgeted (megabytes for a full corpus): redirect it to a file.",
        [f"{PROG} index dump --index build/os-optics-lens-index.sqlite > dump.json"])
    d.set_defaults(handler=_cmd_index_dump)

    p = _add_command(
        subparsers, common, "help", "Print a help topic (Markdown text).",
        "Topics:\n" + "\n".join(f"  {name:<11} {text}" for name, text in HELP_TOPICS.items()),
        [f"{PROG} help quickstart", f"{PROG} help semantics"])
    p.add_argument("topic", nargs="?", metavar="TOPIC", help="one of: " + ", ".join(HELP_TOPICS))
    p.set_defaults(handler=_cmd_help)
    return parser


# ---------------------------------------------------------------------------------------------
# Output


def _stdout_buffer() -> Any:
    return getattr(sys.stdout, "buffer", None)


_CLOSED_PIPE_ERRNOS = frozenset({errno.EPIPE, errno.EINVAL})


def write_bytes(data: bytes) -> None:
    """Write bytes to stdout unchanged (no newline translation, no console encoding).

    A reader that went away (`| head`) surfaces as BrokenPipeError: on Windows a write or flush to
    a closed pipe raises OSError EINVAL instead, which is mapped to BrokenPipeError here."""
    try:
        buffer = _stdout_buffer()
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:  # a text-only stdout (embedding); only ASCII reaches this path for JSON output
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as exc:
        if exc.errno in _CLOSED_PIPE_ERRNOS:
            raise BrokenPipeError(exc.errno, "stdout reader closed the pipe") from None
        raise


def emit(response: dict[str, Any], *, pretty: bool = False) -> None:
    """Write one response document (compact or `--pretty`)."""
    write_bytes(output.serialize(response, pretty=pretty))


# ---------------------------------------------------------------------------------------------
# Input decoding and quick-flag parsing


def _invalid(message: str, hint: str | None = None, **details: Any) -> LensError:
    return LensError("INVALID_REQUEST", message, hint=hint, details=details)


def decode_document(data: bytes, source: str) -> str:
    """Decode request bytes by BOM sniffing: UTF-32/UTF-16 with BOM, UTF-8 with or without BOM."""
    boms = (
        (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
    )
    for bom, encoding in boms:
        if data.startswith(bom):
            body, name = data[len(bom):], encoding
            break
    else:
        body, name = data, "utf-8"
    try:
        return body.decode(name)
    except UnicodeDecodeError as exc:
        raise _invalid(f"{source} is not valid {name.upper()} text (byte {exc.start + len(data) - len(body)})",
                       "Save the request as UTF-8 (a UTF-16 file needs its byte-order mark).",
                       source=source, encoding=name) from None


def parse_document(text: str, source: str, *, positional: bool = False) -> Any:
    """Parse a JSON request document; NaN/Infinity and duplicate member names are rejected.

    A positional argument that looks like JSON with its quotes removed by the shell gets a hint to
    use `--request FILE`.
    """
    try:
        return loads_strict(text, reject_duplicate_keys=True)
    except JsonFormatError as exc:
        details: dict[str, Any] = {"source": source}
        if getattr(exc, "pointer", None):
            details["pointer"] = exc.pointer
        hint = "Check the JSON syntax; `os-optics-lens help request` shows the format."
        if positional and _QUOTE_STRIPPED.match(text):
            hint = ("The shell probably removed the double quotes from the JSON (PowerShell and cmd do). "
                    "Write the request to a file and pass --request FILE, or read it from stdin with -.")
            details["received"] = text[:200]
        raise _invalid(f"{source} is not valid JSON: {exc}", hint, **details) from None


def _read_file(path: str, flag: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        raise _invalid(f"{flag} file not found: {path}", f"Pass an existing file to {flag}.",
                       flag=flag, path=str(Path(path).absolute())) from None
    except IsADirectoryError:
        raise _invalid(f"{flag} names a directory: {path}", f"Pass a file to {flag}.", flag=flag, path=path) from None
    except PermissionError:
        raise _invalid(f"{flag} file is not readable: {path}", None, flag=flag, path=path) from None
    except OSError as exc:
        raise _invalid(f"cannot read {flag} file {path}: {exc.strerror}", None, flag=flag, path=path) from None


def _number(text: str, flag: str, raw: str, expected: str) -> int | float:
    try:
        value = loads_strict(text.strip())
    except JsonFormatError:
        value = None
    if not is_number(value):
        raise _invalid(f"{flag} {raw!r}: {text!r} is not a finite number", f"use {flag} {expected}",
                       flag=flag, value=raw, expected=expected)
    return value


def _comma_list(values: Sequence[str] | None) -> list[str] | None:
    """Repeated and comma-separated values, in order, empty items dropped."""
    if not values:
        return None
    items = [part.strip() for value in values for part in value.split(",")]
    return [item for item in items if item]


def _split_assignment(raw: str, flag: str, expected: str) -> tuple[str, str]:
    name, sep, rest = raw.partition("=")
    if not sep or not name.strip():
        raise _invalid(f"{flag} {raw!r} is not {expected}", f"use {flag} {expected}",
                       flag=flag, value=raw, expected=expected)
    return name.strip(), rest.strip()


def parse_filter_flags(args: argparse.Namespace) -> dict[str, Any] | None:
    """The `filters` object from --filter, --range, --spectral-span and --record-id (None if none)."""
    filters: dict[str, Any] = {}
    facets: dict[str, list[str]] = {}
    for raw in getattr(args, "filter", None) or []:
        name, rest = _split_assignment(raw, "--filter", "FACET=VALUE[,VALUE]")
        values = [v.strip() for v in rest.split(",") if v.strip()]
        if not values:
            raise _invalid(f"--filter {raw!r} has no value", "use --filter FACET=VALUE[,VALUE]",
                           flag="--filter", value=raw)
        bucket = facets.setdefault(name, [])
        bucket.extend(v for v in values if v not in bucket)
    if facets:
        filters["facets"] = facets
    ranges = []
    for raw in getattr(args, "range", None) or []:
        fact, rest = _split_assignment(raw, "--range", "FACT=MIN:MAX")
        if rest.count(":") != 1:
            raise _invalid(f"--range {raw!r} needs MIN:MAX (either end may be empty)", "use --range FACT=MIN:MAX",
                           flag="--range", value=raw, expected="FACT=MIN:MAX")
        low_text, high_text = rest.split(":")
        item: dict[str, Any] = {"fact": fact}
        if low_text.strip():
            item["min"] = _number(low_text, "--range", raw, "FACT=MIN:MAX")
        if high_text.strip():
            item["max"] = _number(high_text, "--range", raw, "FACT=MIN:MAX")
        ranges.append(item)
    if ranges:
        filters["ranges"] = ranges
    span = getattr(args, "spectral_span", None)
    if span is not None:
        if span.count(":") != 1 or not all(part.strip() for part in span.split(":")):
            raise _invalid(f"--spectral-span {span!r} needs LOW:HIGH in nm", "e.g. --spectral-span 450:650",
                           flag="--spectral-span", value=span)
        low_text, high_text = span.split(":")
        filters["spectral_span"] = [_number(low_text, "--spectral-span", span, "LOW:HIGH"),
                                    _number(high_text, "--spectral-span", span, "LOW:HIGH")]
    record_ids = getattr(args, "record_id", None)
    if record_ids:
        filters["record_ids"] = list(dict.fromkeys(record_ids))
    return filters or None


def parse_where(raw: str) -> dict[str, Any]:
    """`--where 'FACT OP VALUE'` -> shorthand constraint.

    The text is split on the first two whitespace runs; VALUE is parsed as JSON, else taken as a
    bare string. `has_source`/`has_sourced_method` take no value.
    """
    parts = _WHERE_SPLIT.split(raw.strip(), maxsplit=2)
    expected = "'FACT OP VALUE'"
    if len(parts) == 2 and parts[1] in _SOURCE_OPS:
        return {"fact": parts[0], "op": parts[1]}
    if len(parts) != 3 or not all(parts):
        raise _invalid(f"--where {raw!r} is not {expected}",
                       "e.g. --where 'first_order.effective_focal_length_mm between_inclusive [17.1,18.9]'",
                       flag="--where", value=raw, expected=expected)
    fact, op, value_text = parts
    try:
        value: Any = loads_strict(value_text)
    except JsonFormatError:
        value = value_text
    return {"fact": fact, "op": op, "value": value}


def parse_target(raw: str) -> dict[str, Any]:
    """`--target FACT=VALUE[~TOL|~PCT%]` -> starting-point target (absolute or relative tolerance)."""
    expected = "FACT=VALUE[~TOL|~PCT%]"
    fact, rest = _split_assignment(raw, "--target", expected)
    value_text, sep, tol_text = rest.partition("~")
    target: dict[str, Any] = {"fact": fact, "target": _number(value_text, "--target", raw, expected)}
    if sep:
        tol_text = tol_text.strip()
        if tol_text.endswith("%"):
            target["relative_tolerance"] = _number(tol_text[:-1], "--target", raw, expected) / 100
        else:
            target["tolerance"] = _number(tol_text, "--target", raw, expected)
    return target


def parse_prefer(raw: str) -> dict[str, Any]:
    """`--prefer FACT[:asc|desc]` -> preference (ascending by default)."""
    fact, sep, direction = raw.strip().rpartition(":")
    if not sep:
        fact, direction = raw.strip(), "asc"
    if direction.lower() not in _DIRECTIONS or not fact:
        raise _invalid(f"--prefer {raw!r} is not FACT[:asc|desc]", "e.g. --prefer design.powered_element_count:asc",
                       flag="--prefer", value=raw, expected="FACT[:asc|desc]")
    return {"fact": fact, "direction": _DIRECTIONS[direction.lower()]}


# ---------------------------------------------------------------------------------------------
# Commands


def _budget(args: argparse.Namespace) -> int | None:
    return getattr(args, "max_output_bytes", None)


def _index_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "index", None)


def _cmd_info(args: argparse.Namespace) -> dict[str, Any]:
    budget = output.check_max_output_bytes(_budget(args), "--max-output-bytes")
    with open_index(_index_arg(args)) as index:
        meta = index.meta()
        result = {
            "package": {"name": PROG, "version": __version__},
            "index": {"path": str(index.path), "source": index.source},
            "meta": meta,
            "build_warnings": len(index.findings()),
            "commands": ["facets", "fields", "search", "compare", "inspect", "project", "index", "help"],
            "next": f"Run `{PROG} help quickstart`, then `{PROG} facets` to see what the collection holds.",
        }
        response = {"ok": True, "snapshot": output.snapshot_info(index), "result": result}
    return output.finalize(response, budget)


def _cmd_facets(args: argparse.Namespace) -> dict[str, Any]:
    filters = parse_filter_flags(args)
    names = _comma_list(args.facet)
    limit = args.limit if args.limit is not None else query.DEFAULT_FACET_LIMIT
    with open_index(_index_arg(args)) as index:
        return query.facets(index, filters, names, limit, describe=args.describe,
                            max_output_bytes=_budget(args))


def _cmd_fields(args: argparse.Namespace) -> dict[str, Any]:
    filters = parse_filter_flags(args)
    limit = args.limit if args.limit is not None else query.DEFAULT_FIELDS_LIMIT
    with open_index(_index_arg(args)) as index:
        return query.fields(index, filters, fact=args.fact, prefix=args.prefix, limit=limit,
                            max_output_bytes=_budget(args))


_BUILDER_FLAGS = ("mode", "where", "target", "prefer", "include", "hard_shortfall",
                  "filter", "range", "spectral_span", "record_id")


def _load_search_request(args: argparse.Namespace) -> Any:
    """The request document named by the positional argument, --request or --fixture (or None)."""
    sources = [name for name, value in (("REQUEST_JSON", args.request_json), ("--request", args.request),
                                        ("--fixture", args.fixture)) if value is not None]
    if len(sources) > 1:
        raise _invalid("give the request once, not as " + " and ".join(sources),
                       "Use exactly one of REQUEST_JSON (or - for stdin), --request FILE or --fixture FILE.",
                       sources=sources)
    if args.fixture is not None:
        case = parse_document(decode_document(_read_file(args.fixture, "--fixture"), "--fixture"), "--fixture")
        return query.fixture_to_request(case)
    if args.request is not None:
        return parse_document(decode_document(_read_file(args.request, "--request"), "--request"), "--request")
    if args.request_json is None:
        return None
    if args.request_json == "-":
        stdin = getattr(sys.stdin, "buffer", None)
        data = stdin.read() if stdin is not None else sys.stdin.read().encode("utf-8")
        return parse_document(decode_document(data, "stdin"), "stdin")
    text = args.request_json
    if not text.lstrip().startswith("{"):
        raise _invalid("REQUEST_JSON must be a JSON object, or - to read stdin",
                       "To read a request from a file use --request FILE.", received=text[:200])
    return parse_document(text, "REQUEST_JSON", positional=True)


def _request_from_flags(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode is None:
        raise _invalid("search needs a request: give REQUEST_JSON, --request FILE, --fixture FILE or --mode "
                       "with quick flags",
                       f"e.g. {PROG} search --mode starting_point --filter design_type=camera_lens "
                       "--target first_order.effective_focal_length_mm=24~3; see `os-optics-lens help request`.",
                       flag="--mode")
    request: dict[str, Any] = {"mode": args.mode}
    filters = parse_filter_flags(args)
    if filters:
        request["filters"] = filters
    if args.where:
        request["constraints"] = [parse_where(raw) for raw in args.where]
    if args.target:
        request["targets"] = [parse_target(raw) for raw in args.target]
    if args.prefer:
        request["preferences"] = [parse_prefer(raw) for raw in args.prefer]
    include = _comma_list([args.include] if args.include is not None else None)
    if include is not None:
        request["include"] = include
    if args.hard_shortfall is not None:
        request["hard_shortfall"] = args.hard_shortfall
    return request


def _apply_overrides(request: Any, args: argparse.Namespace) -> Any:
    overrides: dict[str, Any] = {}
    fields = _comma_list(args.fields)
    if fields is not None:
        overrides["fields"] = fields
    for name in ("detail", "limit", "cursor"):
        if getattr(args, name) is not None:
            overrides[name] = getattr(args, name)
    if not overrides:
        return request
    if isinstance(request, dict) and "queries" in request:
        flags = ["--" + name for name in overrides]
        raise _invalid(f"{', '.join(flags)} apply to a single query, not a batch",
                       "Set limit, cursor, detail and fields inside each queries[i].", flags=flags)
    if isinstance(request, dict):
        request.update(overrides)
    return request


def _cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    request = _load_search_request(args)
    if request is None:
        request = _request_from_flags(args)
    else:
        used = ["--" + name.replace("_", "-") for name in _BUILDER_FLAGS if getattr(args, name, None)]
        if used:
            raise _invalid(f"{', '.join(used)} cannot be combined with a request document",
                           "Put filters, constraints, targets and preferences in the JSON request, or drop "
                           "the document and build the query from flags.", flags=used)
    request = _apply_overrides(request, args)
    with open_index(_index_arg(args)) as index:
        if args.explain_request:
            return query.explain_request(index, request, max_output_bytes=_budget(args))
        return query.run_request(index, request, max_output_bytes=_budget(args))


def _cmd_compare(args: argparse.Namespace) -> dict[str, Any]:
    with open_index(_index_arg(args)) as index:
        return query.compare(index, args.refs, _comma_list(args.fields), max_output_bytes=_budget(args))


def _cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    with open_index(_index_arg(args)) as index:
        return query.inspect(
            index, args.record_id, revision=args.revision, configuration=args.configuration,
            fields=_comma_list(args.fields), sections=_comma_list(args.sections),
            evidence=_comma_list(args.evidence), prescription=args.prescription, provenance=args.provenance,
            max_output_bytes=_budget(args))


def _out_error(path: Path, exc: OSError) -> LensError:
    """INVALID_REQUEST naming --out, the absolute path and the OS reason of a failed write."""
    reason = exc.strerror or type(exc).__name__
    for parent in path.parents:
        if parent.exists():
            if not parent.is_dir():
                reason = f"parent {parent} is not a directory"
            break
    if isinstance(exc, PermissionError):
        reason = f"permission denied ({exc.strerror or 'access is denied'})"
    return _invalid(f"cannot write --out {path}: {reason}",
                    "Choose another --out path (a writable file in an existing or creatable directory).",
                    flag="--out", path=str(path), os_error=exc.strerror or type(exc).__name__)


def _write_project(path: Path, data: bytes, force: bool, sha256: str) -> str:
    """Write `data` to `path` (parents created) and return the SHA-256 of the bytes read back.

    The bytes are written to a temporary file and read back first; only when they hash to `sha256`
    does the temporary file replace `path`, so a mismatch or a failure never leaves a file behind.
    Filesystem failures are INVALID_REQUEST errors naming the path."""
    if path.is_dir():
        raise _invalid(f"--out names a directory: {path}", "Pass a file path to --out.", path=str(path))
    if path.exists() and not force:
        raise LensError("FILE_EXISTS", f"{path} already exists", hint="Pass --force to overwrite it, or "
                        "choose another --out path.", details={"path": str(path)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _out_error(path, exc) from None
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
        written = hashlib.sha256(temp.read_bytes()).hexdigest()
        if written != sha256:
            raise LensError("INTERNAL", f"the bytes written for {path} do not have the Project's sha256; "
                            "nothing was written", details={"path": str(path), "sha256": sha256,
                                                            "written_sha256": written})
        os.replace(temp, path)
    except OSError as exc:
        _remove_quietly(temp)
        raise _out_error(path, exc) from None
    except BaseException:
        _remove_quietly(temp)
        raise
    return written


def _remove_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _cmd_project(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.force and args.out is None:
        raise _invalid("--force applies only with --out", "Use --out FILE --force.", flag="--force")
    if args.stdout and _budget(args) is not None:
        raise _invalid("--max-output-bytes does not apply with --stdout (the raw Project bytes are not "
                       "budgeted)", "Drop --max-output-bytes, or use --out FILE.", flag="--max-output-bytes")
    # Validated before any I/O, so a bad budget never follows a written file.
    output.check_max_output_bytes(_budget(args), "max_output_bytes")
    with open_index(_index_arg(args)) as index:
        if args.stdout:
            data = query.project_bytes(index, args.record_id, revision=args.revision)
            write_bytes(data)
            return None
        if args.out is None:
            return query.project_response(index, args.record_id, revision=args.revision,
                                          max_output_bytes=_budget(args))
        metadata = query.project_metadata(index, args.record_id, revision=args.revision)
        data = query.project_bytes(index, args.record_id, revision=args.revision)
        target = Path(args.out).absolute()
        written = _write_project(target, data, args.force, metadata["sha256"])
        return query.project_response(index, args.record_id, revision=args.revision,
                                      extra={"path": str(target), "written_sha256": written},
                                      max_output_bytes=_budget(args))


def _build_response(result: Any) -> dict[str, Any]:
    """Envelope for `index build`/`index validate`, listing as many warning findings as fit."""
    body = result.to_dict()
    findings = body.pop("findings")
    snapshot = {"id": result.snapshot_id, "records": result.records, "configurations": result.configurations}
    shown = min(len(findings), MAX_LISTED_FINDINGS)
    while True:
        payload = dict(body, findings=findings[:shown], findings_omitted=len(findings) - shown)
        try:
            return output.finalize({"ok": True, "snapshot": snapshot, "result": payload})
        except LensError:
            if shown == 0:
                raise
            shown //= 2


def _bounded_build_failure(error: LensError) -> LensError:
    """BUILD_FAILED with at most MAX_LISTED_FINDINGS findings (errors first, then warnings)."""
    findings = error.details.get("findings")
    if not isinstance(findings, list):
        return error
    ordered = [f for f in findings if f.get("severity") == "error"] + \
              [f for f in findings if f.get("severity") != "error"]
    details = dict(error.details, findings=ordered[:MAX_LISTED_FINDINGS],
                   findings_omitted=max(0, len(ordered) - MAX_LISTED_FINDINGS))
    return LensError(error.code, error.message, hint=error.hint, details=details, exit_code=error.exit_code)


def _cmd_index_build(args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = build_index(args.corpus, args.out, allow_unresolved_evidence=args.allow_unresolved_evidence)
    except LensError as error:
        raise _bounded_build_failure(error) if error.code == "BUILD_FAILED" else error
    return _build_response(result)


def _cmd_index_validate(args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = validate_corpus(args.corpus, allow_unresolved_evidence=args.allow_unresolved_evidence)
    except LensError as error:
        raise _bounded_build_failure(error) if error.code == "BUILD_FAILED" else error
    return _build_response(result)


def _cmd_index_dump(args: argparse.Namespace) -> dict[str, Any]:
    with open_index(_index_arg(args)) as index:
        snapshot = output.snapshot_info(index)
        dump = dump_index(index.path)
    return {"ok": True, "snapshot": snapshot, "result": dump}


def help_text(topic: str) -> bytes:
    """The shipped Markdown of a help topic."""
    return resources.files(__package__).joinpath("help").joinpath(f"{topic}.md").read_bytes()


def _cmd_help(args: argparse.Namespace) -> dict[str, Any] | None:
    topic = args.topic
    if topic is None:
        lines = [f"{PROG} help topics:", ""]
        lines += [f"  {name:<11} {text}" for name, text in HELP_TOPICS.items()]
        lines += ["", f"Print one with `{PROG} help <topic>`; start with `{PROG} help quickstart`.",
                  f"Command options: `{PROG} <command> --help`.", ""]
        write_bytes("\n".join(lines).encode("ascii"))
        return None
    if topic not in HELP_TOPICS:
        raise _invalid(f"unknown help topic {topic!r}", f"Topics: {', '.join(HELP_TOPICS)}.",
                       allowed=list(HELP_TOPICS),
                       did_you_mean=difflib.get_close_matches(topic, list(HELP_TOPICS), n=3, cutoff=0.4))
    write_bytes(help_text(topic))
    return None


# ---------------------------------------------------------------------------------------------
# Entry point


def _internal(exc: BaseException) -> LensError:
    return LensError(
        "INTERNAL", f"unexpected internal error ({type(exc).__name__})",
        hint=f"This is a bug. Rerun with {DEBUG_ENV}=1 to print the traceback to stderr and report it.",
        details={"exception": type(exc).__name__},
    )


def _error_budget(args: argparse.Namespace | None, error: LensError) -> int:
    """The byte budget an error envelope must fit: the failing request's own budget when known,
    else a valid --max-output-bytes, else the default (index build/validate have no option)."""
    if isinstance(error.output_budget, int):
        return error.output_budget
    try:
        return output.check_max_output_bytes(getattr(args, "max_output_bytes", None), "--max-output-bytes")
    except LensError:
        return output.DEFAULT_MAX_OUTPUT_BYTES


def run(argv: Sequence[str]) -> int:
    """Parse `argv`, run the command, write its output and return the exit code.

    Every document written to stdout, error envelopes included, fits the byte budget. A closed
    stdout (BrokenPipeError) propagates to `main`, which exits silently."""
    argv = list(argv)
    pretty = "--pretty" in argv
    error: LensError | None = None
    args: argparse.Namespace | None = None
    try:
        args = build_parser().parse_args(argv)
        pretty = bool(getattr(args, "pretty", False))
        handler: Callable[[argparse.Namespace], dict[str, Any] | None] = args.handler
        response = handler(args)
        if response is None:
            return 0
    except UsageError as exc:
        error = LensError(
            "INVALID_REQUEST", f"invalid command line: {exc.message}",
            hint=f"Run `{PROG} --help` or `{PROG} <command> --help` for the options and examples.",
            details={"argv_error": exc.message, "usage": exc.usage})
    except LensError as exc:
        error = exc
    except SystemExit as exc:  # argparse --help
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BrokenPipeError:
        raise  # stdout is gone: nothing more can be written, main() exits silently
    except Exception as exc:  # noqa: BLE001 - every other failure is the INTERNAL envelope
        if os.environ.get(DEBUG_ENV) == "1":
            traceback.print_exc(file=sys.stderr)
        error = _internal(exc)
    if error is not None:
        emit(output.bounded_error_response(error, _error_budget(args, error)), pretty=pretty)
        return error.exit_code
    emit(response, pretty=pretty)
    return output.exit_code(response)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except OSError as exc:
        # The reader went away (e.g. `| head`): BrokenPipeError, or EINVAL on a Windows pipe.
        if not isinstance(exc, BrokenPipeError) and exc.errno not in _CLOSED_PIPE_ERRNOS:
            raise
        # Silence the flush-at-exit error too.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
