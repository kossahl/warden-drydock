#!/usr/bin/env bash
set -Eeuo pipefail

# Run the CI gates in isolated disposable environments with pinned base images.
# Each container copies from a read-only source mount into a private workspace,
# so generated artifacts never alter the caller's checkout.
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
project_name="drydock-review-${RANDOM}-${BASHPID}"
db_container="${project_name}-postgres"
network_name="${project_name}-network"
python_image="python:3.11-bookworm@sha256:35d3a4a3d5e42e02ab916d44513a050689f12c0533d45598d229672503fe77ca"
compatibility_image="python:3.13-bookworm@sha256:933b46a028fd786c9c3d426ebabc237e29a15912231ea8de576e95f0e4f41a4c"
node_image="node:24.11.1-bookworm@sha256:9a2ed90cd91b1f3412affe080b62e69b057ba8661d9844e143a6bbd76a23260f"
postgres_image="postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3"

source_state() {
  git -C "$root_dir" rev-parse HEAD
  git -C "$root_dir" diff --cached --binary | sha256sum
  git -C "$root_dir" diff --binary | sha256sum
  git -C "$root_dir" status --porcelain=v1 --untracked-files=all
}

initial_source_state=$(source_state)
snapshot_dir=$(mktemp -d)
head_index=$(mktemp)
rm -f -- "$head_index"

cleanup() {
  docker rm -f "$db_container" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -rf -- "$snapshot_dir"
  rm -f -- "$head_index"
}
trap cleanup EXIT

# Snapshot HEAD plus tracked index/worktree changes. Untracked files are not
# part of the tested checkout, and every container receives this same snapshot.
# A temporary HEAD index avoids git archive's export-ignore filtering.
GIT_INDEX_FILE="$head_index" git -C "$root_dir" read-tree HEAD
GIT_INDEX_FILE="$head_index" git -C "$root_dir" checkout-index --all --prefix="$snapshot_dir/"
git -C "$root_dir" diff --binary HEAD -- \
  | git -C "$snapshot_dir" apply --allow-empty --whitespace=nowarn -
# Keep governance tests on snapshot-local metadata instead of exposing the
# host repository. The index is rebuilt after applying tracked changes, and a
# credential-free canonical URL is derived from the origin repository slug.
origin_remote=$(git -C "$root_dir" remote get-url origin)
if [[ "$origin_remote" == *"://"* ]]; then
  origin_path=${origin_remote#*://}
  origin_path=${origin_path#*@}
  origin_path=${origin_path#*/}
else
  origin_path=${origin_remote#*@}
  origin_path=${origin_path#*:}
fi
origin_path=${origin_path%/}
origin_path=${origin_path%.git}
origin_repo=${origin_path##*/}
origin_owner=${origin_path%/*}
origin_owner=${origin_owner##*/}
if [ -z "$origin_owner" ] || [ -z "$origin_repo" ] || [ "$origin_owner" = "$origin_path" ]; then
  echo "Cannot derive the origin repository slug for the test snapshot." >&2
  exit 1
fi
origin_url="https://github.com/${origin_owner}/${origin_repo}.git"
git -C "$snapshot_dir" init --quiet
git -C "$snapshot_dir" remote add origin "$origin_url"
# The snapshot contains only archived or tracked-diff content, so force-add
# keeps force-tracked ignored paths in the index without admitting untracked files.
git -C "$snapshot_dir" add --all --force

current_source_state=$(source_state)
if [ "$current_source_state" != "$initial_source_state" ]; then
  echo "The checkout changed while its test snapshot was being created." >&2
  exit 1
fi

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
  "$postgres_image" >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$db_container" pg_isready -U drydock >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$db_container" pg_isready -U drydock >/dev/null 2>&1 || { echo "PostgreSQL did not become ready" >&2; exit 1; }

docker run --rm --network "$network_name" \
  --sysctl net.ipv6.conf.all.disable_ipv6=1 \
  --sysctl net.ipv6.conf.default.disable_ipv6=1 \
  -e "DRYDOCK_TEST_DATABASE_URL=postgresql://drydock:drydock@${db_container}:5432/drydock" \
  -v "$snapshot_dir:/source:ro" --tmpfs /repo:rw,exec,nosuid -w /repo "$python_image" bash -lc '
    set -Eeuo pipefail
    cp -a /source/. /repo/
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
    rm -rf dist
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
  '

docker run --rm \
  -v "$snapshot_dir:/source:ro" --tmpfs /repo:rw,exec,nosuid -w /repo "$compatibility_image" bash -lc '
    set -Eeuo pipefail
    cp -a /source/. /repo/
    git config --global --add safe.directory /repo
    python -m pip install --disable-pip-version-check --upgrade pip ".[dev]"
    python -m unittest discover -s tests -v
    python -m build
  '

docker run --rm \
  -v "$snapshot_dir:/source:ro" --tmpfs /workspace:rw,exec,nosuid -w /workspace \
  "$node_image" bash -lc '
    set -Eeuo pipefail
    cp -a /source/. /workspace/
    cd /workspace/web
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
final_source_state=$(source_state)
if [ "$final_source_state" != "$initial_source_state" ]; then
  echo "The checkout changed while review checks were running." >&2
  exit 1
fi
echo "review checks passed"
