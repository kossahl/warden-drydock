from __future__ import annotations

import os
from pathlib import Path
import unittest
import uuid

from warden_drydock.hosted.operations.migrate import migration_body


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None
    sql = None


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            if in_quote and index + 1 < len(sql) and sql[index + 1] == "'":
                current.append("''")
                index += 2
                continue
            in_quote = not in_quote
            current.append(char)
        elif (
            char == "-"
            and not in_quote
            and index + 1 < len(sql)
            and sql[index + 1] == "-"
        ):
            while index < len(sql) and sql[index] != "\n":
                index += 1
        elif char == ";" and not in_quote:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL migration test is opt-in")
class PostgresMigrationApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = "drydock_test_" + uuid.uuid4().hex[:16]
        self.connection = psycopg.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)
        migration = (
            Path(__file__).parents[3]
            / "warden_drydock"
            / "hosted"
            / "migrations"
            / "0001_revision_projection.sql"
        )
        with self.connection.transaction():
            self.connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
            self.connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            for statement in _split_statements(migration_body(migration)):
                self.connection.execute(statement)

    def tearDown(self) -> None:
        with self.connection.transaction():
            self.connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )
        self.connection.close()

    def test_0001_applies_operational_and_rebuildable_tables_in_isolation(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s "
                "ORDER BY tablename",
                (self.schema,),
            )
            names = [row[0] for row in cursor.fetchall()]
        self.assertEqual(
            {
                "hosted_publication_intent",
                "hosted_campaign_head",
                "hosted_projection_checkpoint",
                "hosted_projection_shadow_checkpoint",
                "hosted_projection_record",
                "hosted_projection_shadow_record",
            },
            set(names),
        )
        self.assertEqual(
            {"hosted_publication_intent", "hosted_campaign_head"},
            {name for name in names if not name.startswith("hosted_projection_")},
        )
        for base, shadow in (
            ("hosted_projection_checkpoint", "hosted_projection_shadow_checkpoint"),
            ("hosted_projection_record", "hosted_projection_shadow_record"),
        ):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name, table_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name IN (%s, %s) "
                    "ORDER BY ordinal_position",
                    (self.schema, base, shadow),
                )
                rows = cursor.fetchall()
            base_columns = [row[0] for row in rows if row[1] == base]
            shadow_columns = [row[0] for row in rows if row[1] == shadow]
            self.assertEqual(base_columns, shadow_columns)

    def test_0001_catalog_keeps_secrets_out_and_requires_digest_identity(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = %s AND column_name = 'provider_secret'",
                (self.schema,),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT table_name, column_name, data_type, character_maximum_length, "
                "is_nullable FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name IN ('hosted_publication_intent', 'hosted_campaign_head', "
                "'hosted_projection_checkpoint', 'hosted_projection_record') "
                "ORDER BY table_name, ordinal_position",
                (self.schema,),
            )
            columns = {
                (row[0], row[1]): (row[2], row[3], row[4]) for row in cursor.fetchall()
            }
        self.assertEqual(
            ("character", 64, "NO"),
            columns[("hosted_publication_intent", "tree_digest")],
        )
        self.assertEqual(
            ("character", 64, "NO"),
            columns[("hosted_publication_intent", "change_digest")],
        )
        self.assertEqual(
            ("character", 64, "NO"),
            columns[("hosted_projection_checkpoint", "projection_digest")],
        )
        self.assertEqual(
            ("character", 64, "NO"),
            columns[("hosted_projection_record", "body_digest")],
        )
        self.assertEqual(
            ("text", None, "NO"), columns[("hosted_publication_intent", "status")]
        )
        self.assertEqual(
            ("text", None, "YES"),
            columns[("hosted_publication_intent", "parent_revision")],
        )

    def test_0001_catalog_pins_head_and_intent_constraints(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = %s::regnamespace AND contype IN ('p', 'u', 'c') "
                "ORDER BY conname",
                (self.schema,),
            )
            constraints = dict(cursor.fetchall())
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s "
                "AND tablename = 'hosted_publication_intent'",
                (self.schema,),
            )
            index_names = {row[0] for row in cursor.fetchall()}
        self.assertEqual(
            "PRIMARY KEY (intent_id)", constraints["hosted_publication_intent_pkey"]
        )
        self.assertEqual(
            "UNIQUE (intent_token)",
            constraints["hosted_publication_intent_intent_token_key"],
        )
        status_checks = {
            name: definition
            for name, definition in constraints.items()
            if name.startswith("hosted_publication_intent_status_check")
        }
        self.assertEqual(1, len(status_checks))
        status_check = next(iter(status_checks.values()))
        for value in ("'pending'", "'finalized'", "'quarantined'"):
            self.assertIn(value, status_check)
        self.assertEqual(
            "PRIMARY KEY (campaign_id)", constraints["hosted_campaign_head_pkey"]
        )
        self.assertEqual(
            "UNIQUE (revision_id)",
            constraints["hosted_campaign_head_revision_id_key"],
        )
        self.assertEqual(
            "PRIMARY KEY (campaign_id, record_id)",
            constraints["hosted_projection_record_pkey"],
        )
        self.assertIn("hosted_publication_intent_pkey", index_names)
        self.assertIn("hosted_publication_intent_intent_token_key", index_names)
        self.assertIn("hosted_publication_intent_token_idx", index_names)


if __name__ == "__main__":
    unittest.main()