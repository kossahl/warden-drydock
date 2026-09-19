#!/usr/bin/env bash
set -Eeuo pipefail

# Run the CI gates in isolated disposable environments with pinned base images.
# Each container copies from a read-only source mount into a private workspace,
# so generated artifacts never alter the caller's checkout.
root_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
unset GIT_INDEX_FILE
project_name="drydock-review-${RANDOM}-${BASHPID}"
db_container="${project_name}-postgres"
network_name="${project_name}-network"
python_image="python:3.11-bookworm@sha256:35d3a4a3d5e42e02ab916d44513a050689f12c0533d45598d229672503fe77ca"
compatibility_image="python:3.13-bookworm@sha256:933b46a028fd786c9c3d426ebabc237e29a15912231ea8de576e95f0e4f41a4c"
node_image="node:24.11.1-bookworm@sha256:9a2ed90cd91b1f3412affe080b62e69b057ba8661d9844e143a6bbd76a23260f"
postgres_image="postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3"

git_diff() {
  GIT_ATTR_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  git -C "$root_dir" \
    -c core.attributesFile=/dev/null \
    -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab \
    diff \
    --no-ext-diff --no-textconv --no-color \
    --src-prefix=a/ --dst-prefix=b/ --line-prefix= \
    --output-indicator-new=+ --output-indicator-old=- \
    --output-indicator-context=' ' "$@"
}

source_state() {
  git -C "$root_dir" rev-parse HEAD
  git -C "$root_dir" remote get-url origin | sha256sum
  git_diff --cached --binary | sha256sum
  git_diff --binary | sha256sum
  git -C "$root_dir" status --porcelain=v1 --untracked-files=all
}

check_source_guards() {
  replacement_refs=$(git -C "$root_dir" for-each-ref --format='%(refname)' refs/replace/)
  if [ -n "$replacement_refs" ]; then
    echo "Cannot test a checkout with Git replacement refs enabled." >&2
    return 1
  fi

  info_attributes=$(git -C "$root_dir" rev-parse --git-path info/attributes)
  case "$info_attributes" in
    /*) ;;
    *) info_attributes="$root_dir/$info_attributes" ;;
  esac
  if [ -s "$info_attributes" ]; then
    echo "Cannot test a checkout with non-repository attributes in $info_attributes." >&2
    return 1
  fi

  untracked_paths=0
  while IFS= read -r -d '' untracked_path; do
    if [ "$untracked_paths" -eq 0 ]; then
      echo "Cannot test a checkout with untracked non-ignored files:" >&2
    fi
    printf '  %q\n' "$untracked_path" >&2
    untracked_paths=$((untracked_paths + 1))
  done < <(git -C "$root_dir" ls-files --others --exclude-standard -z)
  if [ "$untracked_paths" -gt 0 ]; then
    return 1
  fi

hidden_worktree_paths=0
while IFS= read -r -d '' entry; do
  case "${entry:0:1}" in
    h|s|S) hidden_worktree_paths=$((hidden_worktree_paths + 1)) ;;
  esac
done < <(git -C "$root_dir" ls-files -v -z)
if [ "$hidden_worktree_paths" -gt 0 ]; then
  echo "Cannot test a checkout with assume-unchanged or skip-worktree paths ($hidden_worktree_paths found)." >&2
  return 1
fi

fsmonitor_clean_paths=0
while IFS= read -r -d '' entry; do
  if [ "${entry:0:1}" = h ]; then
    fsmonitor_clean_paths=$((fsmonitor_clean_paths + 1))
  fi
done < <(git -C "$root_dir" ls-files -f -z)
if [ "$fsmonitor_clean_paths" -gt 0 ]; then
  echo "Cannot test a checkout with fsmonitor-clean paths ($fsmonitor_clean_paths found)." >&2
  return 1
fi

unmerged_paths=$(git -C "$root_dir" ls-files --unmerged)
if [ -n "$unmerged_paths" ]; then
  echo "Cannot test a checkout with unmerged index entries." >&2
  return 1
fi

# git diff HEAD uses the worktree representation, so an MM path could leave a
# staged change out of the snapshot. Reject any path changed in both views.
staged_worktree_overlap=$(comm -z -12 \
  <(git_diff --cached --name-only -z | sort -z) \
  <(git_diff --name-only -z | sort -z) \
  | wc -c)
if [ "$staged_worktree_overlap" -gt 0 ]; then
  echo "Cannot test a checkout with staged and worktree changes to the same path." >&2
  return 1
fi

# --no-textconv does not disable clean or process filters when Git reads
# worktree changes. Reject tracked paths using configured content filters rather
# than risk snapshotting normalized content instead of the actual worktree.
while IFS= read -r -d '' filter_path \
  && IFS= read -r -d '' filter_attribute \
  && IFS= read -r -d '' filter_value; do
  if [ "$filter_attribute" = filter ] \
    && [ "$filter_value" != unspecified ] \
    && [ "$filter_value" != unset ] \
    && { git -C "$root_dir" config --get "filter.${filter_value}.clean" >/dev/null 2>&1 \
      || git -C "$root_dir" config --get "filter.${filter_value}.process" >/dev/null 2>&1; }; then
    echo "Cannot test a checkout with a configured content filter ($filter_path)." >&2
    return 1
  fi
done < <(
  git -C "$root_dir" ls-files -z \
    | git -C "$root_dir" check-attr --stdin -z filter
)

# Raw blobs do not reproduce built-in checkout conversions such as ident or
# working-tree-encoding. Reject those attributes rather than test the wrong
# bytes while leaving ordinary text/EOL handling to the canonical diff.
while IFS= read -r -d '' attribute_path \
  && IFS= read -r -d '' attribute_name \
  && IFS= read -r -d '' attribute_value; do
  if [ "$attribute_value" != unspecified ] && [ "$attribute_value" != unset ]; then
    echo "Cannot test a checkout with a built-in conversion attribute ($attribute_path: $attribute_name)." >&2
    return 1
  fi
done < <(
  git -C "$root_dir" ls-files -z \
    | git -C "$root_dir" check-attr --stdin -z ident working-tree-encoding
)
}

check_source_guards

initial_source_state=$(source_state)
snapshot_dir=$(mktemp -d)
template_dir=$(mktemp -d)

cleanup() {
  docker rm -f "$db_container" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -rf -- "$snapshot_dir"
  rm -rf -- "$template_dir"
}
trap cleanup EXIT

# Snapshot HEAD plus tracked index/worktree changes. Untracked files are not
# part of the tested checkout, and every container receives this same snapshot.
# Copying the blobs directly avoids git archive's export-ignore filtering and
# checkout-index applying EOL or clean filters.
while IFS= read -r -d '' tree_entry; do
  tree_meta=${tree_entry%%$'\t'*}
  tree_path=${tree_entry#*$'\t'}
  read -r tree_mode tree_type tree_oid <<< "$tree_meta"
  target_path="$snapshot_dir/$tree_path"
  mkdir -p -- "$(dirname -- "$target_path")"
  case "$tree_mode:$tree_type" in
    100644:blob|100755:blob)
      git -C "$root_dir" cat-file blob "$tree_oid" >"$target_path"
      if [ "$tree_mode" = 100755 ]; then
        chmod 755 -- "$target_path"
      else
        chmod 644 -- "$target_path"
      fi
      ;;
    120000:blob)
      link_target=
      IFS= read -r -d '' link_target < <(git -C "$root_dir" cat-file blob "$tree_oid") || true
      ln -s -- "$link_target" "$target_path"
      ;;
    *)
      echo "Unsupported tracked tree entry mode: $tree_mode:$tree_type ($tree_path)" >&2
      exit 1
      ;;
  esac
done < <(git -C "$root_dir" ls-tree -r -z --full-tree HEAD)
git_diff --binary HEAD -- \
  | git -C "$snapshot_dir" apply --allow-empty --whitespace=nowarn -
# Keep governance tests on snapshot-local metadata instead of exposing the
# host repository. The index is rebuilt after applying tracked changes, and a
# credential-free canonical URL is derived from the tracked allowlist artifact.
allowlist_relative_path=".github/coordination-allowlist.json"
if ! git -C "$root_dir" ls-files --error-unmatch -- "$allowlist_relative_path" >/dev/null 2>&1 \
  || [ ! -f "$root_dir/$allowlist_relative_path" ] \
  || [ -L "$root_dir/$allowlist_relative_path" ]; then
  echo "Cannot read the tracked coordination allowlist artifact for the test snapshot." >&2
  exit 1
fi
snapshot_allowlist="$snapshot_dir/$allowlist_relative_path"
if [ ! -f "$snapshot_allowlist" ] || [ -L "$snapshot_allowlist" ]; then
  echo "Cannot read the coordination allowlist artifact from the test snapshot." >&2
  exit 1
fi
if ! canonical_repository=$(python3 - "$snapshot_allowlist" <<'PY'
import json
import re
import sys


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle, object_pairs_hook=reject_duplicate_keys)
except (OSError, UnicodeError, ValueError):
    raise SystemExit(1)

if not isinstance(data, dict) or set(data) != {"coordination_repositories"}:
    raise SystemExit(1)
repositories = data["coordination_repositories"]
if not isinstance(repositories, list) or len(repositories) != 1:
    raise SystemExit(1)
repository = repositories[0]
if not isinstance(repository, str) or not re.fullmatch(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
):
    raise SystemExit(1)
print(repository)
PY
); then
  echo "Cannot derive a single valid repository from the coordination allowlist artifact." >&2
  exit 1
fi
origin_url="https://github.com/${canonical_repository}.git"
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
  git -C "$snapshot_dir" init --quiet --template="$template_dir"
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
  git -C "$snapshot_dir" remote add origin "$origin_url"
# The snapshot contains only archived or tracked-diff content, so force-add
# keeps force-tracked ignored paths in the index without admitting untracked files.
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
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
  git_diff --check "$merge_base...HEAD"
  git_diff --check
  git_diff --cached --check
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
    python -m pip install --disable-pip-version-check --upgrade pip ".[dev,postgres]"
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
check_source_guards
final_source_state=$(source_state)
if [ "$final_source_state" != "$initial_source_state" ]; then
  echo "The checkout changed while review checks were running." >&2
  exit 1
fi
echo "review checks passed"
