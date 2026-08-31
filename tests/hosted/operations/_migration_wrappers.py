"""Raw-content policy checks for migration SQL files.

``migration_body`` strips a leading ``BEGIN;`` and a trailing ``COMMIT;``
before returning the body, so wrapper detection must read the raw file
instead of the body. The contract is statement-based: the raw content is
split into SQL statements on ``;`` after ``--`` comments are stripped to
end of line. Blank lines and comment-only lines are skipped while locating
the first and last statements, and an inline trailing comment on a
statement line is ignored, so ``BEGIN; -- wrapper`` reads as ``BEGIN;`` and
a ``-- header`` comment above a ``BEGIN;`` does not hide it. A wrapper
hidden on the same line as another statement (for example ``BEGIN; CREATE
TABLE example(id integer);``) is still the first statement and is rejected.
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


def _meaningful_statements(content: str) -> list[str]:
    """Split raw SQL into normalized statement tokens terminated by ``;``.

    ``--`` comments are stripped to end of line before splitting on ``;`` so
    a semicolon inside a comment cannot create a phantom statement; blank and
    comment-only lines then fall out as empty splits and are skipped. Each
    statement is uppercased, whitespace runs collapse to single spaces, and a
    space before the ``;`` is dropped (``COMMIT ;`` reads as ``COMMIT;``).

    Heuristic limitation: ``--`` and ``;`` inside a single-quoted string
    literal are treated as SQL structure, not string content. For wrapper
    detection this cannot raise a false positive unless a string literally
    begins a statement with ``BEGIN`` or ``COMMIT``; a full SQL lexer is not
    warranted here.
    """
    comment_stripped = "\n".join(line.split("--", 1)[0] for line in content.splitlines())
    statements: list[str] = []
    for part in comment_stripped.split(";"):
        token = " ".join(part.split()).upper()
        if token:
            statements.append(f"{token};")
    return statements


def migration_boundary_lines(path: pathlib.Path) -> tuple[str, str]:
    """Return the first and last statement tokens, normalized.

    Each token is uppercased with whitespace runs collapsed and ends with
    ``;``. Blank lines, full-line comments, and inline trailing comments are
    removed before splitting on ``;``. Empty strings mean the raw file
    carried no SQL statements.
    """
    statements = _meaningful_statements(path.read_text(encoding="utf-8"))
    return (statements[0], statements[-1]) if statements else ("", "")


def assert_no_outer_transaction_wrapper(path: pathlib.Path) -> None:
    """Raise AssertionError when the raw file carries an outer transaction wrapper."""
    first, last = migration_boundary_lines(path)
    if first in _TRANSACTION_STARTS:
        raise AssertionError(f"{path}: first SQL statement is {first} (outer transaction wrapper)")
    if last in _TRANSACTION_ENDS:
        raise AssertionError(f"{path}: last SQL statement is {last} (outer transaction wrapper)")