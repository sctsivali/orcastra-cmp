#!/usr/bin/env bash
# Run every job of .github/workflows/ci.yml on this machine.
#
# Hosted CI runs only on pull requests and by hand (see the note at the top of ci.yml), so this is
# the gate before a direct push. The guard list, the Node, Python, PostgreSQL and Playwright
# versions, and the browser job's environment are read from ci.yml rather than copied, so a step
# added there runs here without editing this file.
#
#   scripts/ci-local.sh [stage...]   commits guards backend frontend e2e (default: all, in that order)
#   E2E_PROJECTS="desktop-light"     Playwright projects (default: all four, like a full manual run)
#   CI_LOCAL_BASE=github/dev         lint the messages of CI_LOCAL_BASE..HEAD (default: the upstream)
#   CI_LOCAL_LOG_DIR=path            one log per step (default: under $TMPDIR, printed at the end)
#   CI_LOCAL_POSTGRES=skip           skip the throwaway journal database (it needs docker)
#
# note: this tests the working tree while CI tests the commit, so a run with uncommitted or
# untracked files says so up front. Toolchains live in ~/.cache/orcastra-ci-local, never in the repo.
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel) || exit 2
cd "$ROOT" || exit 2
WF=.github/workflows/ci.yml
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/orcastra-ci-local
LOG=${CI_LOCAL_LOG_DIR:-${TMPDIR:-/tmp}/orcastra-ci-local/$(git rev-parse --short HEAD)}
STAGES=" ${*:-commits guards backend frontend e2e} "
PROJECTS=${E2E_PROJECTS:-desktop-light mobile-dark desktop-dark mobile-light}
failed=0
STEP_LOG=
PG_CONTAINER=
for stage in $STAGES; do
  case $stage in
    commits | guards | backend | frontend | e2e) ;;
    *) echo "unknown stage '$stage' (commits guards backend frontend e2e)" >&2; exit 2 ;;
  esac
done

mkdir -p "$LOG/guards" "$CACHE" || exit 2
: > "$LOG/summary.txt"
exec 9> "$CACHE/run.lock"
if command -v flock > /dev/null && ! flock -n 9; then
  echo "another ci-local run is in progress" >&2
  exit 2
fi
trap '[ -n "$PG_CONTAINER" ] && docker rm -f "$PG_CONTAINER" > /dev/null 2>&1' EXIT
trap 'exit 130' INT TERM

note() { printf '%s\n' "$*" | tee -a "$LOG/summary.txt"; }
selected() { [[ $STAGES == *" $1 "* ]]; }
wf() { grep -m1 -oE "$1" "$WF" | sed -E "s|$1|\\1|"; }
sum256() { if command -v sha256sum > /dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

# A failing step never stops the run, the same as the jobs (fail-fast: false).
step() {
  local label=$1 dir=$2 start=$SECONDS
  shift 2
  STEP_LOG="$LOG/$(printf '%s' "$label" | tr -c 'A-Za-z0-9.-' '_').log"
  printf '...   %s\n' "$label"
  if (cd "$dir" && "$@") > "$STEP_LOG" 2>&1; then
    note "PASS  $label ($((SECONDS - start))s)"
  else
    failed=$((failed + 1))
    note "FAIL  $label ($((SECONDS - start))s)  $STEP_LOG"
    return 1
  fi
}

# Reinstall only when a manifest or the toolchain changed since the last install, like a CI cache.
fresh() { local stamp=$1; shift; [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$(cat "$@" | git hash-object --stdin)" ]; }
stamp() { local stamp=$1; shift; cat "$@" | git hash-object --stdin > "$stamp"; }

use_node() {
  local want os arch dir tarball base
  want=$(wf "node-version: '([0-9.]+)'")
  [ "$(node --version 2> /dev/null)" = "v$want" ] && return 0
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  case $(uname -m) in
    x86_64 | amd64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) note "FAIL  no Node $want build for $(uname -m)"; return 1 ;;
  esac
  dir="$CACHE/node-v$want-$os-$arch"
  if [ ! -x "$dir/bin/node" ]; then
    tarball="node-v$want-$os-$arch.tar.gz"
    base="https://nodejs.org/dist/v$want"
    note "installing Node $want, the version CI uses, into $CACHE"
    if ! (cd "$CACHE" && curl -fsSL "$base/$tarball" -o "$tarball" \
      && curl -fsSL "$base/SHASUMS256.txt" | grep " $tarball\$" | sum256 -c - > /dev/null \
      && tar -xzf "$tarball" && rm -f "$tarball"); then
      note "FAIL  could not install Node $want"
      return 1
    fi
  fi
  export PATH="$dir/bin:$PATH"
}

frontend_deps() {
  fresh frontend/node_modules/.ci-local frontend/package-lock.json <(node --version) && return 0
  step "frontend dependencies (npm ci)" frontend npm ci \
    && stamp frontend/node_modules/.ci-local frontend/package-lock.json <(node --version)
}

use_python() {
  local want have reqs=(backend/requirements.txt backend/requirements-dev.txt backend/constraints.txt)
  if [ ! -x .venv/bin/python ]; then
    failed=$((failed + 1))
    note "FAIL  backend needs a virtualenv at .venv (python3 -m venv .venv)"
    return 1
  fi
  export PATH="$ROOT/.venv/bin:$PATH"
  want=$(wf "python-version: '([0-9.]+)'")
  have=$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  [ "$have" = "$want" ] || note "warning: .venv runs Python $have, CI uses $want"
  fresh .venv/.ci-local "${reqs[@]}" && return 0
  step "backend dependencies (pip)" backend python -m pip install -q --disable-pip-version-check -r requirements-dev.txt \
    && stamp .venv/.ci-local "${reqs[@]}"
}

# CI gives the journal contracts a disposable PostgreSQL service migrated to head. Here it is a
# throwaway container on a random loopback port, removed when the run ends.
journal_db() {
  local image port url
  if [ "${CI_LOCAL_POSTGRES:-}" = skip ] || ! docker info > /dev/null 2>&1; then
    note "SKIP  journal PostgreSQL contracts (CI_LOCAL_POSTGRES=skip or docker unreachable)"
    return 0
  fi
  image=$(wf "image: (postgres:[0-9A-Za-z.-]+)")
  PG_CONTAINER="orcastra-ci-local-pg-$$"
  step "journal database ($image)" . docker run -d --rm --name "$PG_CONTAINER" -p 127.0.0.1::5432 \
    -e POSTGRES_USER=orcastra_test -e POSTGRES_PASSWORD=orcastra_test -e POSTGRES_DB=orcastra_journal_test \
    "$image" || return 1
  # TCP rather than the socket: the image's init server answers on the socket and then restarts.
  for _ in $(seq 60); do
    docker exec "$PG_CONTAINER" pg_isready -h 127.0.0.1 -U orcastra_test -d orcastra_journal_test > /dev/null 2>&1 && break
    sleep 1
  done
  port=$(docker port "$PG_CONTAINER" 5432/tcp | head -1 | sed 's/.*://')
  url="postgresql+asyncpg://orcastra_test:orcastra_test@127.0.0.1:$port/orcastra_journal_test"
  step "journal database migration" backend env DATABASE_URL="$url" alembic upgrade head \
    && export JOURNAL_TEST_DATABASE_URL="$url"
}

stage_commits() {
  local base=${CI_LOCAL_BASE:-$(git rev-parse -q --verify '@{u}' 2> /dev/null)}
  if [ -z "$base" ]; then
    note "SKIP  commit messages (no upstream; set CI_LOCAL_BASE=<remote>/<branch>)"
  elif [ "$(git rev-list --count "$base..HEAD")" = 0 ]; then
    note "SKIP  commit messages (nothing ahead of $base)"
  else
    step "commit messages ($base..HEAD)" frontend npx commitlint --from "$(git rev-parse "$base")" --to HEAD
  fi
}

stage_guards() {
  local cmd name n=0 bad=0
  while read -r cmd; do
    name=${cmd#node scripts/checks/}
    name=${name%.mjs .}
    n=$((n + 1))
    # shellcheck disable=SC2086 # the command is a word list read from ci.yml
    if ! $cmd > "$LOG/guards/$name.log" 2>&1; then
      bad=$((bad + 1))
      note "FAIL  guard $name  $LOG/guards/$name.log"
    fi
  done < <(sed -n '/^  guards:/,/^  [a-z_-]*:$/p' "$WF" | grep -oE "run: node scripts/checks/[a-z0-9-]+\.mjs \.$" | sed 's/^run: //')
  failed=$((failed + bad))
  if [ "$n" -eq 0 ]; then
    failed=$((failed + 1))
    note "FAIL  no guard steps found in $WF"
  elif [ "$bad" -eq 0 ]; then
    note "PASS  structural guards ($n)"
  fi
}

stage_backend() {
  step "backend lint" backend ruff check app/
  step "qa artifact contracts" . make lint-qa-artifacts test-qa-artifacts
  journal_db
  step "backend tests" . make test-backend PYTEST="python -m pytest -q -rs"
  if [ -n "${JOURNAL_TEST_DATABASE_URL:-}" ] && grep -q "JOURNAL_TEST_DATABASE_URL is not set" "$STEP_LOG"; then
    failed=$((failed + 1))
    note "FAIL  the journal PostgreSQL contracts were skipped although the database was up"
  fi
}

stage_frontend() {
  step "frontend typecheck" frontend npx tsc --noEmit
  step "frontend lint" frontend ./node_modules/.bin/eslint src --ext .ts,.tsx
  step "frontend build" frontend env NEXT_TELEMETRY_DISABLED=1 npm run build
}

stage_e2e() {
  local version dir line port p limit=()
  version=$(wf "@playwright/test@([0-9.]+)")
  dir="$CACHE/playwright-$version"
  if [ ! -x "$dir/node_modules/.bin/playwright" ]; then
    step "install Playwright $version" . npm install --prefix "$dir" --no-save --no-package-lock "@playwright/test@$version" || return
  fi
  step "install Chromium" . "$dir/node_modules/.bin/playwright" install chromium || return
  # The browser job's env block, then a free port: CI's 2025 is usually a running dev server here.
  while IFS= read -r line; do
    export "${line?}"
  done < <(sed -n '/^  browser-e2e:/,/^    steps:/p' "$WF" | sed -n '/^    env:/,/^    steps:/p' \
    | sed -nE "s/^      ([A-Z_][A-Z0-9_]*): '?([^']*)'?\$/\\1=\\2/p")
  port=${E2E_PORT:-$(node -e 'const s=require("net").createServer().listen(0,"127.0.0.1",()=>{console.log(s.address().port);s.close()})')}
  export ORCASTRA_E2E_BASE_URL="http://127.0.0.1:$port" NEXTAUTH_URL="http://127.0.0.1:$port"
  step "e2e build" frontend npm run build || return
  command -v timeout > /dev/null && limit=(timeout 45m)
  for p in $PROJECTS; do
    step "e2e $p" frontend env ORCASTRA_E2E_ARTIFACT_ROOT="$LOG/e2e-$p" NODE_PATH="$dir/node_modules" \
      ${limit[@]+"${limit[@]}"} "$dir/node_modules/.bin/playwright" test --config=playwright.config.cjs --project="$p"
    note "      $(grep -E '^ +[0-9]+ (passed|failed|flaky|skipped|did not run)' "$STEP_LOG" | tr -s ' ' | tr '\n' ' ')"
  done
}

note "ci-local on $(git rev-parse --short HEAD), stages:$STAGES"
[ -n "$(git status --porcelain)" ] \
  && note "warning: uncommitted or untracked files are part of this run; CI would test the commit alone"

node_ok=1
if selected commits || selected guards || selected frontend || selected e2e; then
  { use_node && frontend_deps; } || node_ok=0
fi
for stage in commits guards backend frontend e2e; do
  selected "$stage" || continue
  if [ "$stage" != backend ] && [ "$node_ok" != 1 ]; then
    failed=$((failed + 1))
    note "FAIL  $stage (the Node toolchain or frontend dependencies are not ready)"
    continue
  fi
  case $stage in
    commits) stage_commits ;;
    guards) stage_guards ;;
    backend) use_python && stage_backend ;;
    frontend) stage_frontend ;;
    e2e) stage_e2e ;;
  esac
done

note "logs: $LOG"
if [ "$failed" -eq 0 ]; then
  note "ci-local: all green"
else
  note "ci-local: $failed failure(s)"
fi
exit $((failed > 0 ? 1 : 0))
