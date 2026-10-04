#!/usr/bin/env bash
# Create or rotate the write-only service account a stack's Fluent Bit uses. Runs ON the host.
#
# Usage: scripts/provision-stack.sh <stack> <env> [--rotate]
#   prereq: role writer_<stack>_<env> exists in opensearch/security/roles.yml (+ mapping) and
#           scripts/apply-security.sh has been run. See docs/onboarding-a-stack.md.
# Output: secrets/fluentbit-<stack>-<env>.password (mode 600). The password is never printed;
#         copy it to the stack host over SSH (runbook).
source "$(dirname "$0")/lib.sh"

stack="${1:?stack}"; env="${2:?env}"; rotate=0; [[ "${3:-}" == "--rotate" ]] && rotate=1
[[ "$stack" =~ ^[a-z0-9]+$ && "$env" =~ ^[a-z0-9]+$ ]] || die "stack/env must be [a-z0-9]+"
user="fluentbit-$stack-$env"; role="writer_${stack}_${env}"
pwfile="$ORCA_LOGGING_DIR/secrets/$user.password"

os GET "/_plugins/_security/api/roles/$role" >/dev/null 2>&1 || die "role $role missing: add it to roles.yml and run apply-security.sh"

umask 077
if [[ ! -s "$pwfile" || $rotate == 1 ]]; then
  # 32 chars of [A-Za-z0-9]: no quoting issues in Fluent Bit config or env files.
  # note: no `tr </dev/urandom | head` pipeline: under pipefail the early-exiting head kills tr
  # with SIGPIPE and set -e aborts silently. No trailing newline either: it would be part of the password.
  python3 -c 'import secrets,string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)), end="")' > "$pwfile.new"
  [[ "$(wc -c < "$pwfile.new")" -eq 32 ]] || die "password generation failed"
  mv "$pwfile.new" "$pwfile"
  info "generated password for $user"
fi
body="$(python3 -c 'import json,sys; print(json.dumps({"password": open(sys.argv[1]).read(), "backend_roles": [sys.argv[2]], "description": "Fluent Bit writer for " + sys.argv[3]}))' "$pwfile" "$role" "$stack/$env")"
os PUT "/_plugins/_security/api/internalusers/$user" -H 'content-type: application/json' -d @- <<<"$body" >/dev/null
info "user $user -> backend role $role"

# Prove the account authenticates and holds exactly the writer role.
curl -sS --fail-with-body -m 20 "${_TLS[@]}" -u "$user:$(cat "$pwfile")" "$OS_URL/_plugins/_security/authinfo" \
  | pyj "assert '$role' in d['roles'], d['roles']; print('    authinfo ok:', d['user_name'], d['roles'])"
