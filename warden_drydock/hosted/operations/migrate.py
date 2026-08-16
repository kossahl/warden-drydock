from __future__ import annotations

import os
import pathlib
import subprocess


LOCK_ID = "8231649237461"


def migration_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def migration_body(path: pathlib.Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip().upper() == "BEGIN;":
        lines = lines[1:]
    if lines and lines[-1].strip().upper() == "COMMIT;":
        lines = lines[:-1]
    return "\n".join(lines)


def run_migrations(database_url: str, root: pathlib.Path) -> None:
    files = migration_files(root)
    if not files:
        raise RuntimeError("migration_set_empty")
    bootstrap = (
        "CREATE TABLE IF NOT EXISTS hosted_schema_migration ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"
    )
    subprocess.run(["psql", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-c", bootstrap], check=True)
    for path in files:
        version = path.name.split("_", 1)[0]
        wrapper = (
            f"BEGIN; SELECT pg_advisory_xact_lock({LOCK_ID});\n"
            f"SELECT NOT EXISTS (SELECT 1 FROM hosted_schema_migration WHERE version = '{version}') AS apply \\gset\n"
            "\\if :apply\n"
            + migration_body(path)
            + f"\nINSERT INTO hosted_schema_migration(version) VALUES ('{version}');\n"
            + "\\endif\nCOMMIT;\n"
        )
        subprocess.run(
            ["psql", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=wrapper, text=True, check=True,
        )


def main() -> None:
    run_migrations(
        os.environ["DATABASE_URL"],
        pathlib.Path(os.environ.get("DRYDOCK_MIGRATIONS", pathlib.Path(__file__).parents[1] / "migrations")),
    )


if __name__ == "__main__":
    main()
