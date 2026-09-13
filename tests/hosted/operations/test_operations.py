from __future__ import annotations

import json
import io
import os
import pathlib
import tarfile
import tempfile
import unittest
import uuid

import yaml

from tests.hosted.operations._migration_wrappers import assert_no_outer_transaction_wrapper
from warden_drydock.hosted.operations.migrate import migration_body, migration_files
from warden_drydock.hosted.operations.recovery import build_manifest, create_snapshot_archive, extract_snapshot_archive, safe_members, snapshot_archive_inventory, verify_manifest
from warden_drydock.hosted.operations.runtime_guard import parse_version, require_minimum
from warden_drydock.hosted.operations.secrets import SecretStore


ROOT = pathlib.Path(__file__).parents[3]

# Opt-in live PostgreSQL gate matching tests.hosted.http.test_postgres_http:
# the migration-execution regression tests below require a live database and the
# psycopg driver, and skip cleanly when either is unavailable.
DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None


MIGRATIONS_ROOT = ROOT / "warden_drydock" / "hosted" / "migrations"
PRE_0010_MIGRATIONS = (
    "0003_ai_live_backend.sql",
    "0008_live_end_barrier_provenance.sql",
    "0009_live_session_current_pointer.sql",
)


def _migration_body(name: str) -> str:
    return migration_body(MIGRATIONS_ROOT / name)


class ComposePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    def test_two_services_and_only_app_publishes_loopback(self) -> None:
        services = self.compose["services"]
        self.assertEqual({"app", "db"}, set(services))
        publishers = [name for name, service in services.items() if service.get("ports")]
        self.assertEqual(["app"], publishers)
        self.assertEqual("127.0.0.1", services["app"]["ports"][0]["host_ip"])

    def test_db_has_no_egress_or_host_port(self) -> None:
        db = self.compose["services"]["db"]
        self.assertNotIn("ports", db)
        self.assertEqual(["backend"], db["networks"])
        self.assertTrue(self.compose["networks"]["backend"]["internal"])
        self.assertIn("/proc/1/comm", self.compose["services"]["db"]["healthcheck"]["test"][1])

    def test_containers_are_hardened(self) -> None:
        for service in self.compose["services"].values():
            self.assertTrue(service["read_only"])
            self.assertEqual(["ALL"], service["cap_drop"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            self.assertIn("limits", service["deploy"]["resources"])
            self.assertIn("tmpfs", service)
            self.assertNotIn("/var/run/docker.sock", json.dumps(service))

    def test_secrets_are_not_environment_values(self) -> None:
        rendered = json.dumps(self.compose["services"])
        self.assertNotIn("POSTGRES_PASSWORD\"", rendered)
        app = self.compose["services"]["app"]
        self.assertNotIn("OPENAI_API_KEY", app["environment"])
        self.assertEqual(
            "/var/lib/drydock/secrets/openai_api_key",
            app["environment"]["OPENAI_API_KEY_FILE"],
        )
        self.assertIn("provider_secrets:/var/lib/drydock/secrets", app["volumes"])
        self.assertNotIn("provider_secrets:/var/lib/drydock/secrets", self.compose["services"]["db"]["volumes"])
        self.assertNotIn("sk-", rendered)
        for service in self.compose["services"].values():
            self.assertEqual(["20000"], service["group_add"])
            self.assertIn("database_secrets:/run/secrets:ro", service["volumes"])

    def test_provider_credential_stays_out_of_image_and_browser_sources(self) -> None:
        dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
        browser_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "web" / "src").rglob("*")
            if path.is_file()
        )
        self.assertNotIn("OPENAI_API_KEY", dockerfile)
        self.assertNotIn("OPENAI_API_KEY", browser_sources)
        self.assertIn("database_secrets", self.compose["volumes"])


class RuntimeTests(unittest.TestCase):
    def test_versions(self) -> None:
        self.assertEqual((28, 5, 2), parse_version("Docker version 28.5.2"))
        self.assertEqual((28, 5, 2), parse_version("28.5.2"))
        require_minimum("28.5.2", (28, 5, 2), "docker")
        require_minimum("28.5.3", (28, 5, 2), "docker")
        require_minimum("v28.0.0", (28, 0, 0), "docker")
        with self.assertRaisesRegex(RuntimeError, "unsupported_docker_version"):
            require_minimum("28.5.1", (28, 5, 2), "docker")
        with self.assertRaisesRegex(RuntimeError, "unsupported_docker_version"):
            require_minimum("27.5.1", (28, 0, 0), "docker")
        for malformed in ("Docker version", "Docker version 28.5", "Docker version 28.x.5"):
            with self.assertRaisesRegex(ValueError, "unrecognized_version"):
                parse_version(malformed)

    def test_migrations_are_ordered_and_outer_transactions_removed(self) -> None:
        files = migration_files(ROOT / "warden_drydock" / "hosted" / "migrations")
        self.assertEqual(["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012"], [path.name[:4] for path in files])
        for path in files:
            assert_no_outer_transaction_wrapper(path)

    def test_migration_wrapper_check_reads_raw_content_not_stripped_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wrapped = root / "0001_wrapped.sql"
            wrapped.write_text("BEGIN;\nCREATE TABLE example(id integer);\nCOMMIT;\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as begin_failure:
                assert_no_outer_transaction_wrapper(wrapped)
            self.assertIn(str(wrapped), str(begin_failure.exception))
            self.assertIn("BEGIN;", str(begin_failure.exception))
            trailing_commit = root / "0002_trailing_commit.sql"
            trailing_commit.write_text("CREATE TABLE example(id integer);\nCOMMIT;\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as commit_failure:
                assert_no_outer_transaction_wrapper(trailing_commit)
            self.assertIn(str(trailing_commit), str(commit_failure.exception))
            self.assertIn("COMMIT;", str(commit_failure.exception))
            clean = root / "0003_no_wrapper.sql"
            clean.write_text("CREATE TABLE example(id integer);\n", encoding="utf-8")
            assert_no_outer_transaction_wrapper(clean)

    def test_wrapper_check_tolerates_inline_and_full_line_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inline_wrapped = root / "0001_inline_comment_wrapped.sql"
            inline_wrapped.write_text(
                "BEGIN; -- note\nCREATE TABLE example(id integer);\nCOMMIT; -- note\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as begin_failure:
                assert_no_outer_transaction_wrapper(inline_wrapped)
            self.assertIn(str(inline_wrapped), str(begin_failure.exception))
            self.assertIn("BEGIN;", str(begin_failure.exception))
            inline_trailing = root / "0002_inline_comment_trailing.sql"
            inline_trailing.write_text(
                "-- header\nCREATE TABLE example(id integer);\nCOMMIT; -- note\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as commit_failure:
                assert_no_outer_transaction_wrapper(inline_trailing)
            self.assertIn(str(inline_trailing), str(commit_failure.exception))
            self.assertIn("COMMIT;", str(commit_failure.exception))
            commented_wrapped = root / "0003_header_above_wrapper.sql"
            commented_wrapped.write_text(
                "-- header\nBEGIN;\nCREATE TABLE example(id integer);\nCOMMIT;\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as begin_failure:
                assert_no_outer_transaction_wrapper(commented_wrapped)
            self.assertIn(str(commented_wrapped), str(begin_failure.exception))
            self.assertIn("BEGIN;", str(begin_failure.exception))
            commented_trailing = root / "0004_header_above_trailing.sql"
            commented_trailing.write_text(
                "-- header\nCREATE TABLE example(id integer);\nCOMMIT;\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as commit_failure:
                assert_no_outer_transaction_wrapper(commented_trailing)
            self.assertIn(str(commented_trailing), str(commit_failure.exception))
            self.assertIn("COMMIT;", str(commit_failure.exception))
            comment_clean = root / "0005_comment_then_clean.sql"
            comment_clean.write_text(
                "-- header\n\nDELETE FROM hosted_http_operation_receipt;\n",
                encoding="utf-8",
            )
            assert_no_outer_transaction_wrapper(comment_clean)

    def test_wrapper_check_covers_transaction_start_and_end_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index, start in enumerate(
                ("BEGIN;", "BEGIN WORK;", "BEGIN TRANSACTION;", "START TRANSACTION;"),
                start=1,
            ):
                path = root / f"{index:04d}_start.sql"
                path.write_text(f"{start} -- note\nCREATE TABLE example(id integer);\n", encoding="utf-8")
                with self.assertRaises(AssertionError) as failure:
                    assert_no_outer_transaction_wrapper(path)
                self.assertIn(str(path), str(failure.exception))
                self.assertIn(start, str(failure.exception))
            for index, end in enumerate(
                ("COMMIT;", "COMMIT WORK;", "COMMIT TRANSACTION;"),
                start=10,
            ):
                path = root / f"{index:04d}_end.sql"
                path.write_text(f"-- header\nCREATE TABLE example(id integer);\n{end} -- note\n", encoding="utf-8")
                with self.assertRaises(AssertionError) as failure:
                    assert_no_outer_transaction_wrapper(path)
                self.assertIn(str(path), str(failure.exception))
                self.assertIn(end, str(failure.exception))

    def test_wrapper_check_rejects_wrappers_hidden_on_shared_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            shared_wrappers = root / "0001_shared_line_wrappers.sql"
            shared_wrappers.write_text(
                "BEGIN; CREATE TABLE example(id integer);\n"
                "CREATE TABLE other(id integer); COMMIT;\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as begin_failure:
                assert_no_outer_transaction_wrapper(shared_wrappers)
            self.assertIn(str(shared_wrappers), str(begin_failure.exception))
            self.assertIn("BEGIN;", str(begin_failure.exception))
            shared_trailing_commit = root / "0002_shared_line_trailing_commit.sql"
            shared_trailing_commit.write_text(
                "\nCREATE TABLE example(id integer);\n"
                "CREATE TABLE other(id integer); COMMIT;\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as commit_failure:
                assert_no_outer_transaction_wrapper(shared_trailing_commit)
            self.assertIn(str(shared_trailing_commit), str(commit_failure.exception))
            self.assertIn("COMMIT;", str(commit_failure.exception))
            shared_clean = root / "0003_shared_line_clean.sql"
            shared_clean.write_text(
                "CREATE TABLE example(id integer); CREATE TABLE other(id integer);\n",
                encoding="utf-8",
            )
            assert_no_outer_transaction_wrapper(shared_clean)

    def test_readiness_requires_v2_receipt_reset_schema(self) -> None:
        health = (ROOT / "warden_drydock" / "hosted" / "operations" / "health.py").read_text(encoding="utf-8")
        self.assertIn("version='0007'", health)
        self.assertNotIn("version='0002'", health)
        self.assertIn("version='0011'", health)
        self.assertIn("version='0012'", health)

    def test_v2_migration_resets_only_transport_receipts(self) -> None:
        migration = (ROOT / "warden_drydock" / "hosted" / "migrations" / "0007_http_v2_receipt_reset.sql").read_text(encoding="utf-8")
        self.assertIn("DELETE FROM hosted_http_operation_receipt", migration)
        for protected in (
            "hosted_campaign", "hosted_revision", "hosted_proposal_version",
            "hosted_proposal_audit", "hosted_ai_generation", "hosted_live_session",
        ):
            self.assertNotIn(f"DELETE FROM {protected}", migration)
            self.assertNotIn(f"DROP TABLE {protected}", migration)

    def test_editor_receipt_migration_allows_durable_editor_operations(self) -> None:
        migration = (ROOT / "warden_drydock" / "hosted" / "migrations" / "0012_editor_http_receipts.sql").read_text(encoding="utf-8")
        for operation in (
            "editor_record_create", "editor_record_edit", "editor_record_remove",
            "editor_proposal_correct", "editor_proposal_reject", "editor_proposal_approve",
        ):
            self.assertIn(f"'{operation}'", migration)

    def test_secret_replace_is_atomic_and_metadata_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(pathlib.Path(directory))
            fingerprint = store.replace("provider", b"sensitive")
            self.assertEqual({"present": True, "credential_revision": fingerprint}, store.metadata("provider"))
            self.assertNotIn("sensitive", repr(store.metadata("provider")))
            if os.name == "posix":
                self.assertEqual(0o600, (pathlib.Path(directory) / "provider").stat().st_mode & 0o777)
            store.remove("provider")
            self.assertFalse(store.metadata("provider")["present"])

    def test_backup_hashes_and_unsafe_archive_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "postgres.dump").write_bytes(b"database")
            with tarfile.open(root / "snapshots.tar", "w") as archive:
                info = tarfile.TarInfo("snapshots/example")
                info.size = len(b"snapshots")
                archive.addfile(info, io.BytesIO(b"snapshots"))
            manifest = build_manifest(root / "postgres.dump", root / "snapshots.tar", snapshot_archive_inventory(root / "snapshots.tar"))
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(manifest, verify_manifest(root))
            incomplete = dict(manifest)
            incomplete["files"] = {"snapshots.tar": manifest["files"]["snapshots.tar"]}
            (root / "manifest.json").write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "backup_file_set_mismatch"):
                verify_manifest(root)
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "postgres.dump").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "backup_digest_mismatch"):
                verify_manifest(root)
        with self.assertRaisesRegex(ValueError, "unsafe_backup_member"):
            safe_members([tarfile.TarInfo("../escape")])
        with self.assertRaisesRegex(ValueError, "unsafe_backup_member"):
            safe_members([tarfile.TarInfo("secrets/provider")])
        with self.assertRaisesRegex(ValueError, "unsafe_backup_member"):
            safe_members([tarfile.TarInfo("snapshots/a"), tarfile.TarInfo("snapshots/./a")])
        with self.assertRaisesRegex(ValueError, "unsafe_backup_member"):
            safe_members([tarfile.TarInfo("snapshots/a"), tarfile.TarInfo("snapshots")])

    def test_snapshot_archive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "snapshot.txt").write_text("content", encoding="utf-8")
            archive = root / "snapshots.tar"
            inventory = create_snapshot_archive(source, archive)
            self.assertEqual(inventory, snapshot_archive_inventory(archive))
            restored = extract_snapshot_archive(archive, root)
            self.assertEqual("content", (restored / "nested" / "snapshot.txt").read_text(encoding="utf-8"))

    def test_build_context_excludes_real_secrets(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("docker/secrets/*.txt", ignore)
        self.assertIn(".git/**", ignore)

    def test_recovery_scripts_fail_closed(self) -> None:
        backup = (ROOT / "docker" / "backup.ps1").read_text(encoding="utf-8")
        restore = (ROOT / "docker" / "restore.ps1").read_text(encoding="utf-8")
        self.assertIn("docker compose stop app", backup)
        self.assertIn("pending publication intents", backup)
        self.assertIn("Assert-NativeSuccess", backup)
        self.assertIn("/var/lib/postgresql/data/.drydock-backup.dump", backup)
        self.assertLess(backup.index("docker compose stop app"), backup.index("pg_dump"))
        self.assertLess(backup.index("docker compose stop app"), backup.index("docker compose cp app:/var/lib/drydock/snapshots"))
        self.assertIn("Restore volume already exists", restore)
        self.assertIn("Restore project already has containers", restore)
        self.assertIn("docker volume ls --quiet", restore)
        self.assertIn("finally", restore)
        self.assertIn("/var/lib/postgresql/data/.drydock-restore.dump", restore)
        self.assertLess(restore.index("up -d --wait db"), restore.index("pg_restore"))
        self.assertLess(restore.index("pg_restore"), restore.index("snapshot restore copy"))
        self.assertLess(restore.index("snapshot restore copy"), restore.index("application startup"))
        initializer = (ROOT / "docker" / "initialize-secrets.ps1").read_text(encoding="utf-8")
        self.assertIn("cmp -s", initializer)
        self.assertIn("440 0:20000", initializer)
        provider = (ROOT / "docker" / "manage-provider-secret.ps1").read_text(encoding="utf-8")
        self.assertIn("Read-Host 'OpenAI API key' -AsSecureString", provider)
        self.assertLess(provider.index("AsSecureString"), provider.index("SecretStore"))
        self.assertIn("SecretStore", provider)
        self.assertIn("ZeroFreeBSTR", provider)
        self.assertIn("OpenAIResponsesAdapter().verify()", provider)
        self.assertNotIn("OPENAI_API_KEY=", provider)


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL migration 0010 regression test is opt-in")
class PostgresMigration0010RegressionTests(unittest.TestCase):
    """Executes migration 0010_live_session_monotonic_order.sql against a real
    hosted_live_session table (isolated in a unique scratch schema) and asserts
    the pre-release policy:

      * non-empty table FAILS CLOSED (pre_release_live_session_reset_required),
        the whole transaction rolls back (no column/index/marker change, data
        unchanged), and readiness stays closed because the '0010' migration
        marker is absent (health.readiness() requires that marker); and
      * an empty table succeeds, adding the bigint sequence-backed session_seq
        (NOT NULL, nextval default) plus the (campaign_id, session_seq DESC)
        ordering index and the '0010' marker.

    The migration body is applied through psycopg inside the same
    BEGIN/COMMIT + marker-insert transaction wrapper the migrate.run_migrations
    runner uses, so a raise in the leading DO-block aborts the whole
    transaction (rollback). No psql binary is required. Each test builds state
    in a scratch schema and drops it in tearDown.
    """

    def setUp(self) -> None:
        self.schema = "drydock_migr_" + uuid.uuid4().hex[:24]
        connection = psycopg.connect(DATABASE_URL, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        finally:
            connection.close()
        self.connect = lambda: psycopg.connect(DATABASE_URL)

    def tearDown(self) -> None:
        connection = self.connect()
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            connection.close()

    def _session(self):
        """A fresh connection whose search_path resolves to the scratch schema."""
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
        except BaseException:
            connection.close()
            raise
        return connection

    def _build_pre_0010_state(self, connection) -> None:
        """Recreate the token-identical pre-0010 hosted_live_session schema and the
        hosted_schema_migration table inside the scratch schema."""
        with connection.cursor() as cursor:
            for name in PRE_0010_MIGRATIONS:
                cursor.execute(_migration_body(name))
            cursor.execute(
                "CREATE TABLE hosted_schema_migration ("
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )

    _MINIMAL_SESSION_ROW = (
        "INSERT INTO hosted_live_session"
        "(session_id, campaign_id, base_revision, reported_head_revision,"
        " workflow_version, controller_epoch, controller_id, mode)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    _MINIMAL_SESSION_VALUES = ("session_x", "campaign_y", "rev1", "rev1", 1, 1, "controller_a", "active")

    def test_non_empty_table_fails_closed_and_rolls_back(self) -> None:
        # Phase 1: build the pre-0010 schema in a scratch schema and insert a
        # minimal live-session row (schema from 0003/0008/0009), then commit.
        connection = self._session()
        try:
            self._build_pre_0010_state(connection)
            with connection.cursor() as cursor:
                cursor.execute(self._MINIMAL_SESSION_ROW, self._MINIMAL_SESSION_VALUES)
            connection.commit()

            # Phase 2: apply migration 0010. The leading DO-block must fail
            # closed while the table is non-empty, aborting the transaction.
            with connection.cursor() as cursor:
                with self.assertRaisesRegex(psycopg.Error, "pre_release_live_session_reset_required"):
                    cursor.execute(_migration_body("0010_live_session_monotonic_order.sql"))
            connection.rollback()
        finally:
            connection.close()

        # Phase 3: fresh session verifies the failure transaction left no trace.
        check = self._session()
        try:
            with check.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='hosted_live_session' "
                    "AND column_name='session_seq'"
                )
                self.assertEqual(0, cursor.fetchone()[0], "session_seq column must not be applied on failure")
                cursor.execute(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE schemaname=current_schema() AND indexname='hosted_live_session_campaign_seq_idx'"
                )
                self.assertEqual(0, cursor.fetchone()[0], "ordering index must not be applied on failure")
                cursor.execute(
                    "SELECT count(*) FROM pg_class WHERE oid=to_regclass('hosted_live_session_seq_seq')"
                )
                self.assertEqual(0, cursor.fetchone()[0], "backing sequence must not be created on failure")
                cursor.execute("SELECT count(*) FROM hosted_schema_migration WHERE version='0010'")
                self.assertEqual(0, cursor.fetchone()[0], "'0010' marker must be absent on failure (readiness closed)")
                cursor.execute("SELECT session_id, mode FROM hosted_live_session")
                self.assertEqual(("session_x", "active"), cursor.fetchone(), "pre-existing row must be unchanged")
        finally:
            check.close()

    def test_empty_table_applies_sequence_default_and_ordering_index(self) -> None:
        # Phase 1: pre-0010 schema with an EMPTY hosted_live_session table.
        connection = self._session()
        try:
            self._build_pre_0010_state(connection)
            connection.commit()
            # Phase 2: apply migration 0010 on empty data plus the runner's marker insert.
            with connection.cursor() as cursor:
                cursor.execute(_migration_body("0010_live_session_monotonic_order.sql"))
                cursor.execute("INSERT INTO hosted_schema_migration(version) VALUES ('0010')")
            connection.commit()
        finally:
            connection.close()

        # Phase 3: fresh session verifies the additive result and that the
        # sequence default actually assigns increasing session_seq values.
        check = self._session()
        try:
            with check.cursor() as cursor:
                cursor.execute(
                    "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='hosted_live_session' "
                    "AND column_name='session_seq'"
                )
                data_type, is_nullable, column_default = cursor.fetchone()
                self.assertEqual("bigint", data_type)
                self.assertEqual("NO", is_nullable)
                self.assertIn("nextval", column_default)
                cursor.execute(
                    "SELECT count(*) FROM pg_class WHERE oid=to_regclass('hosted_live_session_seq_seq')"
                )
                self.assertEqual(1, cursor.fetchone()[0], "backing sequence must exist")
                cursor.execute(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname=current_schema() AND indexname='hosted_live_session_campaign_seq_idx'"
                )
                indexdef = cursor.fetchone()
                self.assertIsNotNone(indexdef, "ordering index must exist")
                self.assertIn("session_seq DESC", indexdef[0])
                cursor.execute("SELECT count(*) FROM hosted_schema_migration WHERE version='0010'")
                self.assertEqual(1, cursor.fetchone()[0], "'0010' marker must be recorded on success")
                # Sequence default assigns strictly increasing, non-null values.
                for session_id in ("session_a", "session_b"):
                    cursor.execute(
                        self._MINIMAL_SESSION_ROW,
                        (session_id, "campaign_y", "rev1", "rev1", 1, 1, "controller_a", "ended"),
                    )
                cursor.execute("SELECT session_seq FROM hosted_live_session WHERE session_id='session_a'")
                first = cursor.fetchone()[0]
                cursor.execute("SELECT session_seq FROM hosted_live_session WHERE session_id='session_b'")
                second = cursor.fetchone()[0]
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                self.assertLess(first, second)
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
