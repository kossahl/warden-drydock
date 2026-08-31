"""Raw-content policy checks for migration SQL files.

``migration_body`` strips a leading ``BEGIN;`` and a trailing ``COMMIT;``
before returning the body, so wrapper detection must read the raw file
instead of the body.
"""

from __future__ import annotations

import pathlib


def migration_boundary_lines(path: pathlib.Path) -> tuple[str, str]:
    """Return the first and last non-blank lines, stripped and uppercased."""
    lines = path.read_text(encoding="utf-8").splitlines()
    first = next((line.strip().upper() for line in lines if line.strip()), "")
    last = next((line.strip().upper() for line in reversed(lines) if line.strip()), "")
    return first, last


def assert_no_outer_transaction_wrapper(path: pathlib.Path) -> None:
    """Raise AssertionError when the raw file carries an outer transaction wrapper."""
    first, last = migration_boundary_lines(path)
    if first == "BEGIN;":
        raise AssertionError(f"{path}: first non-blank line is BEGIN; (outer transaction wrapper)")
    if last == "COMMIT;":
        raise AssertionError(f"{path}: last non-blank line is COMMIT; (outer transaction wrapper)")