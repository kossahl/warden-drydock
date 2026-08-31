from __future__ import annotations

import os
import pathlib
import shutil
import unittest
import uuid

from tests.hosted.operations._migration_wrappers import assert_no_outer_transaction_wrapper
from warden_drydock.hosted.operations.migrate import migration_files, run_migrations


ROOT = pathlib.Path(__file__).parents[3]
MIGRATIONS = ROOT / "warden_drydock" / "hosted" / "migrations"

DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None
PSQL = shutil.which("psql")


@unittest.skipUnless(
    DATABASE_URL and psycopg and PSQL,
    "live PostgreSQL migration test is opt-in (DRYDOCK_TEST_DATABASE_URL, psycopg, psql)",
)
class PostgresMigrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "drydock_migrations_" + uuid.uuid4().hex[:16]
        identifier = psycopg.sql.Identifier(self.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(identifier))
        self.url = psycopg.conninfo.make_conninfo(DATABASE_URL, dbname=self.name)

    def tearDown(self) -> None:
        if not hasattr(self, "name"):
            return
        identifier = psycopg.sql.Identifier(self.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier))

    def test_migrations_apply_in_order_through_real_runner(self) -> None:
        files = migration_files(MIGRATIONS)
        prefixes = [path.name[:4] for path in files]
        for path in files:
            assert_no_outer_transaction_wrapper(path)
        schema_tables = {
            "0001": "hosted_publication_intent",
            "0002": "hosted_runtime_state",
            "0003": "hosted_ai_generation",
            "0004": "hosted_proposal_version",
            "0005": "hosted_http_operation_receipt",
            "0006": "hosted_atlas_record",
        }
        run_migrations(self.url, MIGRATIONS)
        with psycopg.connect(self.url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version FROM hosted_schema_migration ORDER BY version")
                self.assertEqual(prefixes, [row[0] for row in cursor.fetchall()])
                cursor.execute("SELECT EXISTS (SELECT 1 FROM hosted_schema_migration WHERE version='0007')")
                self.assertTrue(cursor.fetchone()[0])
                for prefix, table in schema_tables.items():
                    cursor.execute("SELECT to_regclass(%s)", (table,))
                    self.assertIsNotNone(cursor.fetchone()[0], f"{prefix} did not create {table}")
        run_migrations(self.url, MIGRATIONS)
        with psycopg.connect(self.url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM hosted_schema_migration")
                self.assertEqual(len(prefixes), cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()