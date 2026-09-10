#!/usr/bin/env bash
set -Eeuo pipefail

# Run the CI gates in pinned disposable environments. This keeps results
# independent of the agent host's Node, npm, Python packages, and database.
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
project_name="drydock-review-${RANDOM}-${BASHPID}"
db_container="${project_name}-postgres"
network_name="${project_name}-network"
python_image="python:3.11-bookworm@sha256:35d3a4a3d5e42e02ab916d44513a050689f12c0533d45598d229672503fe77ca"
compatibility_image="python:3.13-bookworm@sha256:933b46a028fd786c9c3d426ebabc237e29a15912231ea8de576e95f0e4f41a4c"
node_image="node:24.11.1-bookworm"
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

check_whitespace() {
  local base_ref="${DRYDOCK_CI_BASE_REF:-}"
  if [ -z "$base_ref" ]; then
    for candidate in origin/master master; do
      if git -C "$root_dir" rev-parse --verify "$candidate^{commit}" >/dev/null 2>&1; then
        base_ref="$candidate"
        break
      fi
    done
  fi
  if [ -z "$base_ref" ]; then
    echo "Cannot determine the CI base revision. Fetch origin/master or set DRYDOCK_CI_BASE_REF." >&2
    return 1
  fi

  local merge_base
  merge_base=$(git -C "$root_dir" merge-base "$base_ref" HEAD)
  git -C "$root_dir" diff --check "$merge_base...HEAD"
  git -C "$root_dir" diff --check
  git -C "$root_dir" diff --cached --check
}

check_whitespace

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
  -v "$root_dir:/repo" -w /repo "$python_image" bash -lc '
    set -Eeuo pipefail
    git config --global --add safe.directory /repo
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends postgresql-client
    python -m pip install --disable-pip-version-check --upgrade pip ".[dev]"
    mkdir -p /tmp/drydock-python-wheels
    python -m pip download --disable-pip-version-check -q --timeout 15 --retries 5 --only-binary=:all: "psycopg[binary]" -d /tmp/drydock-python-wheels
    python -m pip install --disable-pip-version-check -q --no-deps /tmp/drydock-python-wheels/*
    DATABASE_URL="$DRYDOCK_TEST_DATABASE_URL" DRYDOCK_MIGRATIONS=/repo/warden_drydock/hosted/migrations python -m warden_drydock.hosted.operations.migrate
    python -m unittest discover -s tests -v
    python -m warden_drydock --help
    python -m build

    onboarding_root="$(mktemp -d)"
    campaign="$onboarding_root/campaign"
    environment="$onboarding_root/environment"
    wheel="$(find "$PWD/dist" -maxdepth 1 -name "*.whl" -print -quit)"
    test -n "$wheel"
    mkdir "$campaign"
    python -m venv "$environment"
    "$environment/bin/python" -m pip install "$wheel"
    cd "$onboarding_root"
    installed_version="$("$environment/bin/python" -c "from importlib.metadata import version; print(version(\"warden-drydock\"))")"
    test "$("$environment/bin/python" -m warden_drydock --version)" = "Warden Drydock $installed_version"
    "$environment/bin/python" -m warden_drydock bootstrap "$campaign" --adapter mothership --name "CI Onboarding"
    "$environment/bin/python" "$campaign/scripts/drydock.py" validate
    test ! -d "$campaign/.git"

    cd /repo
    python -m unittest tests.hosted.proposals.test_postgres_proposals tests.hosted.http.test_postgres_http
  '

docker run --rm \
  "${git_mount_args[@]}" \
  -v "$root_dir:/repo" -w /repo "$compatibility_image" bash -lc '
    set -Eeuo pipefail
    git config --global --add safe.directory /repo
    python -m pip install --disable-pip-version-check --upgrade pip ".[dev]"
    python -m unittest discover -s tests -v
    python -m build
  '

docker run --rm \
  -v "$root_dir:/workspace" -w /workspace/web \
  "$node_image" bash -lc '
    set -Eeuo pipefail
    corepack enable
    corepack prepare npm@11.6.2 --activate
    test "$(node --version)" = "v24.11.1"
    test "$(npm --version)" = "11.6.2"
    npm ci
    npm run typecheck
    npm run test:unit
    npx playwright install --with-deps chromium
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python-is-python3
    npm run test:browser
    npm run build:reproducible
  '

check_whitespace
echo "review checks passed"
