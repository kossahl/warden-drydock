from __future__ import annotations

import json
import io
import os
import pathlib
import tarfile
import tempfile
import unittest

import yaml

from warden_drydock.hosted.operations.migrate import migration_body, migration_files
from warden_drydock.hosted.operations.recovery import build_manifest, create_snapshot_archive, extract_snapshot_archive, safe_members, snapshot_archive_inventory, verify_manifest
from warden_drydock.hosted.operations.runtime_guard import parse_version, require_minimum
from warden_drydock.hosted.operations.secrets import SecretStore


ROOT = pathlib.Path(__file__).parents[3]


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
        self.assertNotIn("PROVIDER_API_KEY", rendered)


class RuntimeTests(unittest.TestCase):
    def test_versions(self) -> None:
        self.assertEqual((28, 5, 2), parse_version("Docker version 28.5.2"))
        require_minimum("v28.0.0", (28, 0, 0), "docker")
        with self.assertRaisesRegex(RuntimeError, "unsupported_docker_version"):
            require_minimum("27.5.1", (28, 0, 0), "docker")

    def test_migrations_are_ordered_and_outer_transactions_removed(self) -> None:
        files = migration_files(ROOT / "warden_drydock" / "hosted" / "migrations")
        self.assertEqual(["0001", "0002"], [path.name[:4] for path in files])
        body = migration_body(files[0])
        self.assertFalse(body.startswith("BEGIN;"))
        self.assertFalse(body.endswith("COMMIT;"))

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
        self.assertIn("Restore volume already exists", restore)
        self.assertLess(restore.index("snapshot restore copy"), restore.index("application startup"))


if __name__ == "__main__":
    unittest.main()
