# Hosted localhost operations

The pilot requires Docker Engine **28.0.0 or newer** and Docker Compose
**2.20.2 or newer**. Engine 28 is the minimum because it fixed reachability of
localhost-published ports from neighboring L2 hosts. Compose 2.20.2 is the
minimum that supports the health-check timing fields used by `compose.yaml`.
Run this check before startup:

```powershell
python -c "from warden_drydock.hosted.operations.runtime_guard import check_host_runtime; check_host_runtime()"
```

Copy `docker/secrets/db_password.txt.example` to the ignored
`docker/secrets/db_password.txt`, replace its content with a long random local
password, then start the two-service runtime:

```powershell
docker compose config --quiet
docker compose up --build --wait
```

Only `app` publishes `127.0.0.1:8080`. PostgreSQL is internal-only. Use
`docker/compose.ipv6.yaml` only after verifying `::1` binding and LAN isolation
on the target host. Do not add a database port or attach `db` to `egress`.

## Health and migration behavior

Startup obtains a PostgreSQL advisory transaction lock and applies each
packaged migration exactly once. A failed migration rolls back and prevents
`app` startup. `/health/live` reports process health. `/health/ready` requires
the expected schema record plus readable and writable snapshot/secret volumes.
Provider failure is intentionally outside base readiness so deterministic
Capture can remain available.

## Backup

Browser-only unsynchronized data is not in a server backup. After confirming
the browser queue is synchronized, run:

```powershell
./docker/backup.ps1 -Destination ./docker/backups/2026-08-14 -AcknowledgeUnsynchronizedBrowserData
```

The command enters maintenance mode, creates and lists a PostgreSQL custom
dump, archives snapshots, records hashes and inventory identity, verifies the
manifest, excludes provider secrets, and always leaves maintenance mode.

## Restore drill and rollback

Restore to a new Compose project name, never over the running project:

```powershell
./docker/restore.ps1 -Backup ./docker/backups/2026-08-14 -RestoreProject drydock-restore-drill
```

The command verifies hashes, starts fresh named volumes, restores PostgreSQL in
one transaction, validates archive members, restores snapshots, and checks
readiness. The original project and volumes remain the rollback target. Do not
run `docker compose down --volumes` against either project until the Warden has
accepted the restored heads, snapshot inventory, and projection digests.
Provider secrets are excluded; restored service remains behind provider setup
and renewed-consent gates.
