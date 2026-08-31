"""Raw-content wrapper checks for migration SQL files, self-contained for #209.

``migration_body`` strips a leading ``BEGIN;`` and a trailing ``COMMIT;``
before returning the body, so wrapper detection must read the raw file
instead of the body. This mirrors the semantic contract of the migration
source policy in PR #153 (``tests/hosted/operations/_migration_wrappers.py``)
without importing branch-local code: raw text is filtered to meaningful
statement lines, blank lines and full-line ``--`` comments are skipped,
inline ``--`` trailing comments are stripped, the remainder is split on
``;`` into statements, and the first and last statements are treated as
the outer boundaries.
"""

from __future__ import annotations

import pathlib

TRANSACTION_STARTS = frozenset(
    {
        "BEGIN;",
        "BEGIN WORK;",
        "BEGIN TRANSACTION;",
        "START TRANSACTION;",
    }
)
TRANSACTION_ENDS = frozenset(
    {
        "COMMIT;",
        "COMMIT WORK;",
        "COMMIT TRANSACTION;",
    }
)


def _meaningful_lines(lines: list[str]) -> list[str]:
    """Keep statement lines, dropping blanks, full-line comments, and inline comments."""
    meaningful: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        meaningful.append(" ".join(stripped.split("--", 1)[0].split()))
    return meaningful


def migration_boundaries(path: pathlib.Path) -> tuple[str, str]:
    """Return the first and last normalized SQL statements of the raw file.

    Blank lines and full-line comments are skipped, inline trailing comments
    are removed, and whitespace runs collapse. Empty strings mean the file
    carried no SQL statements.
    """
    text = "\n".join(_meaningful_lines(path.read_text(encoding="utf-8").splitlines()))
    tokens = []
    for chunk in text.split(";"):
        if chunk.strip():
            tokens.append(" ".join(chunk.split()).upper() + ";")
    if not tokens:
        return ("", "")
    return (tokens[0], tokens[-1])


def assert_no_outer_transaction_wrapper_raw(path: pathlib.Path) -> None:
    """Raise AssertionError when the raw file carries an outer transaction wrapper."""
    first, last = migration_boundaries(path)
    if first in TRANSACTION_STARTS:
        raise AssertionError(f"{path}: first SQL statement is {first} (outer transaction wrapper)")
    if last in TRANSACTION_ENDS:
        raise AssertionError(f"{path}: last SQL statement is {last} (outer transaction wrapper)")