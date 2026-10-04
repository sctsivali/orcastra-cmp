#!/usr/bin/env bash
# Register the filesystem snapshot repository (idempotent) and take a snapshot.
# Usage: scripts/snapshot.sh <snapshot-name> [index-pattern,...]
# note: the repository lives on the same host and disk (ARCHIVE_DIR). It protects against
# operator mistakes and bad migrations, not against losing the VM. Copy it off-host for DR.
source "$(dirname "$0")/lib.sh"

name="${1:?snapshot name required}"
indices="${2:-orcastra-*,vault-audit-*,security-auditlog-*}"
repo="${SNAPSHOT_REPO:-orcastra-archive}"
[[ "$name" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "snapshot name must be lowercase [a-z0-9._-]: $name"

info "repository $repo"
os PUT "/_snapshot/$repo" -H 'content-type: application/json' \
  -d '{"type":"fs","settings":{"location":"/usr/share/opensearch/snapshots","compress":true}}' >/dev/null
os POST "/_snapshot/$repo/_verify" >/dev/null

info "snapshot $repo/$name ($indices)"
OS_TIMEOUT=3600 os PUT "/_snapshot/$repo/$name?wait_for_completion=true" -H 'content-type: application/json' \
  -d "{\"indices\":\"$indices\",\"include_global_state\":false,\"ignore_unavailable\":true}" | pyj "
s=d['snapshot']; sh=s['shards']
print('    state', s['state'], 'indices', len(s['indices']), 'shards', sh['successful'], '/', sh['total'], 'failed', sh['failed'])
assert s['state']=='SUCCESS' and sh['failed']==0, 'snapshot not clean'"
