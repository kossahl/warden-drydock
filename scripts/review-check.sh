#!/usr/bin/env bash
set -Eeuo pipefail

# Run the review gates in pinned disposable environments. This keeps results
# independent of the agent host's Node, npm, Python packages, and database.
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
project_name="drydock-review-${RANDOM}-${BASHPID}"
db_container="${project_name}-postgres"
network_name="${project_name}-network"
git_mount_args=()
if [ -f "$root_dir/.git" ]; then
  git_common_dir=$(realpath "$(git -C "$root_dir" rev-parse --git-common-dir)")
  git_worktree_dir=$(realpath "$(git -C "$root_dir" rev-parse --git-dir)")
  git_mount_args=(-v "$git_common_dir:$git_common_dir:ro" -v "$git_worktree_dir:$git_worktree_dir:ro")
fi
cleanup() {
  docker rm -f "$db_container" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$network_name" >/dev/null
docker run -d --rm --name "$db_container" \
  --sysctl net.ipv6.conf.all.disable_ipv6=1 \
  --sysctl net.ipv6.conf.default.disable_ipv6=1 \
  --network "$network_name" \
  -e POSTGRES_USER=drydock -e POSTGRES_PASSWORD=drydock -e POSTGRES_DB=drydock \
  postgres:17.6-bookworm >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$db_container" pg_isready -U drydock >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$db_container" pg_isready -U drydock >/dev/null 2>&1 || { echo "PostgreSQL did not become ready" >&2; exit 1; }

docker run --rm --network "$network_name" \
  --sysctl net.ipv6.conf.all.disable_ipv6=1 \
  --sysctl net.ipv6.conf.default.disable_ipv6=1 \
  -e "DRYDOCK_TEST_DATABASE_URL=postgresql://drydock:drydock@${db_container}:5432/drydock" \
  "${git_mount_args[@]}" \
  -v "$root_dir:/repo" -w /repo python:3.12-bookworm bash -lc '
    set -Eeuo pipefail
    git config --global --add safe.directory /repo
    mkdir -p /tmp/drydock-python-wheels
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends postgresql-client
    pip download --disable-pip-version-check -q --timeout 15 --retries 5 --only-binary=:all: "psycopg[binary]" "jsonschema>=4.23" "PyYAML>=6" -d /tmp/drydock-python-wheels
    pip install --disable-pip-version-check -q --no-deps /tmp/drydock-python-wheels/*
    DATABASE_URL="$DRYDOCK_TEST_DATABASE_URL" DRYDOCK_MIGRATIONS=/repo/warden_drydock/hosted/migrations python3 -m warden_drydock.hosted.operations.migrate
    python3 -m unittest discover -s tests
    python3 -m warden_drydock --help >/dev/null
    python3 -m unittest tests.hosted.proposals.test_postgres_proposals tests.hosted.http.test_postgres_http
  '

docker run --rm \
  --sysctl net.ipv6.conf.all.disable_ipv6=1 \
  --sysctl net.ipv6.conf.default.disable_ipv6=1 \
  -v "$root_dir:/workspace" -w /workspace/web \
  node:24.11.1-bookworm bash -lc '
    set -Eeuo pipefail
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
