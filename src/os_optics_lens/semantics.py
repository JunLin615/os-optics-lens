"""Fingerprints of the code and data that give an index and a cursor their meaning.

A fingerprint is the SHA-256 over the named package files, read through `importlib.resources` with
CRLF normalized to LF, so a CRLF checkout, a wheel install and every platform agree.

* `reader_fingerprint` covers what decides query results and their order. Cursors carry it: a
  cursor made by a reader with another fingerprint is rejected (`CURSOR_SEMANTICS_MISMATCH`).
* `index_fingerprint` covers what produces an index. Builds store it as meta `index_semantics`;
  opening an index whose value differs from the reader's is refused (`INDEX_UNAVAILABLE`).
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources

__all__ = ["READER_SOURCES", "INDEX_SOURCES", "fingerprint", "reader_fingerprint", "index_fingerprint"]

READER_SOURCES = ("canonical.py", "evaluate.py", "normalize.py", "output.py", "project_facts.py",
                  "query.py", "store.py", "vocabulary.json", "vocabulary.py")
"""The modules and data that decide query results and their order."""
INDEX_SOURCES = ("build.py", "canonical.py", "corpus.py", "normalize.py", "project_facts.py", "vocabulary.json",
                 "vocabulary.py")
"""The modules and data that produce an index."""


def fingerprint(names: tuple[str, ...]) -> str:
    """SHA-256 over (name, length, LF bytes) of the named files of this package, in name order."""
    digest = hashlib.sha256()
    for name in sorted(names):
        data = resources.files(__package__).joinpath(name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(f"{name}\n{len(data)}\n".encode("ascii"))
        digest.update(data)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def reader_fingerprint() -> str:
    """The fingerprint of this reader's query semantics (64 hex characters, cached)."""
    return fingerprint(READER_SOURCES)


@lru_cache(maxsize=1)
def index_fingerprint() -> str:
    """The fingerprint of the index-producing sources (64 hex characters, cached)."""
    return fingerprint(INDEX_SOURCES)
