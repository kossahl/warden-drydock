from __future__ import annotations

import argparse
import os
import pathlib
import subprocess


def liveness() -> bool:
    return True


def readiness() -> bool:
    for name in ("DRYDOCK_SNAPSHOTS", "DRYDOCK_SECRETS"):
        root = pathlib.Path(os.environ[name])
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK):
            return False
    query = (
        "SELECT 1 FROM hosted_runtime_state r "
        "WHERE r.singleton AND NOT r.maintenance_mode "
        "AND r.reconciliation_complete AND r.schema_compatibility=1 "
        "AND EXISTS (SELECT 1 FROM hosted_schema_migration WHERE version='0002')"
    )
    environment = os.environ.copy()
    secret = pathlib.Path("/run/secrets/db_password")
    if secret.is_file():
        environment["PGPASSWORD"] = secret.read_text(encoding="utf-8").strip()
    result = subprocess.run(
        ["psql", os.environ["DATABASE_URL"], "-X", "-At", "-c", query],
        capture_output=True, text=True, env=environment,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    healthy = readiness() if args.ready else liveness()
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
