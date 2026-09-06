#!/usr/bin/env bash
set -Eeuo pipefail

# Run the review gates in pinned disposable environments. This keeps results
# independent of the agent host's Node, npm, Python packages, and database.
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
project_name="drydock-review-${RANDOM}-${BASHPID}"
db_container="${project_name}-postgres"
cleanup() {
  docker rm -f "$db_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm --name "$db_container" \
  -e POSTGRES_USER=drydock -e POSTGRES_PASSWORD=drydock -e POSTGRES_DB=drydock \
  -p 127.0.0.1::5432 postgres:17.6-bookworm >/dev/null
db_port=""
for _ in $(seq 1 60); do
  db_port=$(docker port "$db_container" 5432/tcp 2>/dev/null | sed -E 's/.*:([0-9]+)$/\1/' || true)
  if [ -n "$db_port" ] && docker exec "$db_container" pg_isready -U drydock >/dev/null 2>&1; then break; fi
  sleep 1
done
[ -n "$db_port" ] || { echo "PostgreSQL did not become ready" >&2; exit 1; }

docker run --rm --network host \
  -e "DRYDOCK_TEST_DATABASE_URL=postgresql://drydock:drydock@127.0.0.1:${db_port}/drydock" \
  -v "$root_dir:/repo" -w /repo python:3.12-bookworm bash -lc '
    mkdir -p /tmp/drydock-python-wheels
    pip download --disable-pip-version-check -q --timeout 15 --retries 5 --only-binary=:all: "psycopg[binary]" -d /tmp/drydock-python-wheels
    pip install --disable-pip-version-check -q --no-deps /tmp/drydock-python-wheels/*
    python3 -m unittest discover -s tests
    python3 -m warden_drydock --help >/dev/null
    python3 -m unittest tests.hosted.proposals.test_postgres_proposals tests.hosted.http.test_postgres_http
  '

docker run --rm \
  -v "$root_dir/web:/workspace" -w /workspace \
  node:24.11.1-bookworm bash -lc '
    corepack enable
    corepack prepare npm@11.6.2 --activate
    npm ci
    npm run typecheck
    npm run test:unit
    npm run build
    npm run build:reproducible
  '

git -C "$root_dir" diff --check
echo "review checks passed"
