"""Raw-content policy checks for migration SQL files.

``migration_body`` strips a leading ``BEGIN;`` and a trailing ``COMMIT;``
before returning the body, so wrapper detection must read the raw file
instead of the body. The contract is statement-based, not line-based:
blank lines and full-line comments are skipped while locating the first
and last statements, and an inline trailing comment on a statement line
is ignored, so ``BEGIN; -- wrapper`` reads as ``BEGIN;`` and a ``-- header``
comment above a ``BEGIN;`` does not hide it.
"""

from __future__ import annotations

import pathlib

_TRANSACTION_STARTS = frozenset(
    {
        "BEGIN;",
        "BEGIN WORK;",
        "BEGIN TRANSACTION;",
        "START TRANSACTION;",
    }
)
_TRANSACTION_ENDS = frozenset(
    {
        "COMMIT;",
        "COMMIT WORK;",
        "COMMIT TRANSACTION;",
    }
)


def _meaningful_statements(lines: list[str]) -> list[str]:
    """Normalize SQL statements, skipping blank and full-line comment lines.

    An inline ``--`` comment is removed before normalization, so
    ``BEGIN; -- note`` becomes ``BEGIN;``. Internal whitespace runs collapse
    to single spaces, and whitespace before a ``;`` is dropped.
    """
    statements: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.lstrip().startswith("--"):
            continue
        statement = " ".join(stripped.split("--", 1)[0].split())
        statements.append(statement.upper().replace(" ;", ";"))
    return statements


def migration_boundary_lines(path: pathlib.Path) -> tuple[str, str]:
    """Return the first and last meaningful statement tokens, normalized.

    Blank lines and full-line comments are skipped. Inline trailing comments
    are removed. Each token is uppercased with whitespace runs collapsed.
    Empty strings mean the file carried no SQL statements.
    """
    statements = _meaningful_statements(path.read_text(encoding="utf-8").splitlines())
    return (statements[0], statements[-1]) if statements else ("", "")


def assert_no_outer_transaction_wrapper(path: pathlib.Path) -> None:
    """Raise AssertionError when the raw file carries an outer transaction wrapper."""
    first, last = migration_boundary_lines(path)
    if first in _TRANSACTION_STARTS:
        raise AssertionError(f"{path}: first SQL statement is {first} (outer transaction wrapper)")
    if last in _TRANSACTION_ENDS:
        raise AssertionError(f"{path}: last SQL statement is {last} (outer transaction wrapper)")