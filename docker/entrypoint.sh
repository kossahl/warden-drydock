#!/bin/sh
set -eu

if [ ! -r /run/secrets/db_password ]; then
    echo 'database credential unavailable' >&2
    exit 1
fi
export PGPASSWORD="$(cat /run/secrets/db_password)"
python -m warden_drydock.hosted.operations.runtime_guard
python -m warden_drydock.hosted.operations.migrate
python -m warden_drydock.hosted.operations.recover
exec "$@"
