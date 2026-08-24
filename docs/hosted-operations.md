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
`docker/secrets/db_password.txt` and replace its content with a long random
local password. Build the application image, initialize the database secret,
and securely enter the OpenAI credential into the provider-secret volume:

```powershell
docker compose config --quiet
docker compose build app
./docker/initialize-secrets.ps1
./docker/manage-provider-secret.ps1 -Action Set
docker compose up --build --wait
./docker/manage-provider-secret.ps1 -Action Verify
```

The initialization step copies the database credential into a project-scoped
Docker volume as `root:20000` with mode `0440`. Both runtime users receive only
that supplemental read group. This avoids Docker Desktop's Windows file-secret
mount behavior, which cannot enforce Compose `uid`, `gid`, or `mode` fields.

`manage-provider-secret.ps1` prompts through `Read-Host -AsSecureString` and
passes the credential over the temporary container's standard input. It never
places the value in the command line, Compose environment, image, tracked file,
PostgreSQL, browser, or script output. The container writes it atomically into
the app-only `provider_secrets` volume through `SecretStore`. The `Verify`
action checks only whether the running adapter sees a non-empty configured
credential. It prints no value and performs no provider request.

To rotate the credential, run the `Set` action again. The atomic replacement
changes the credential fingerprint, so existing consent becomes stale and the
browser requires explicit consent again. To remove provider access, run:

```powershell
./docker/manage-provider-secret.ps1 -Action Remove
```

Removal returns grounded AI to the provider-setup gate. Deterministic campaign
creation and revision browsing remain available. Provider secrets remain
excluded from backup and restore.

Only `app` publishes `127.0.0.1:8080`. PostgreSQL is internal-only. Use
`docker/compose.ipv6.yaml` only after verifying `::1` binding and LAN isolation
on the target host. Do not add a database port or attach `db` to `egress`.

## Health and migration behavior

Startup obtains a PostgreSQL advisory transaction lock and applies each
packaged migration exactly once. A failed migration rolls back and prevents
`app` startup. `/health/live` reports process health. `/health/ready` requires
the expected `0006` schema record plus readable and writable snapshot/secret volumes.
Provider failure is intentionally outside base readiness so deterministic
Capture can remain available.

## Backup

Browser-only unsynchronized data is not in a server backup. After confirming
the browser queue is synchronized, run:

```powershell
./docker/backup.ps1 -Destination ./docker/backups/2026-08-14 -AcknowledgeUnsynchronizedBrowserData
```

The command stops `app` as the write barrier, rejects unresolved publication
intents, enters maintenance mode, creates and lists a PostgreSQL custom dump,
copies the stopped snapshot volume, archives it, records hashes and inventory
identity, verifies the manifest, excludes provider secrets, and restarts the
application. A failed native command terminates the workflow.

## Restore drill and rollback

Restore to a new Compose project name, never over the running project:

```powershell
./docker/restore.ps1 -Backup ./docker/backups/2026-08-14 -RestoreProject drydock-restore-drill
```

The command fails if the restore project or any of its expected volumes already
exists. It uses an ephemeral host port so the original app can remain available
as the rollback target. It verifies hashes, starts fresh named volumes, restores PostgreSQL in
one transaction, restricts archive members to the snapshot namespace, restores
snapshots before normal service, reconciles intent bindings, rebuilds
projections, and only then enables readiness. The original project and volumes
remain the rollback target. Do not
run `docker compose down --volumes` against either project until the Warden has
accepted the restored heads, snapshot inventory, and projection digests.
Database and provider secrets are excluded; restore initializes a fresh
database-secret volume from current local configuration. The restored service remains behind provider setup
and renewed-consent gates.
