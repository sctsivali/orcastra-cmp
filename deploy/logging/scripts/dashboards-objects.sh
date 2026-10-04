#!/usr/bin/env bash
# Create the shared Dashboards objects in the Global tenant (where SSO users land). Idempotent.
# Runs ON the host against the local Dashboards (break-glass admin, basic auth).
#   containers-* index pattern, with its field list refreshed from the current mapping
#   dashboards/saved-objects/containers.ndjson: "Container Logs" dashboard, visualizations, searches
# The Orcastra dashboards (orcastra-*, vault-audit-*) come from orcastra-cmp's
# setup_opensearch_dashboards.sh and must be imported into the Global tenant as well.
source "$(dirname "$0")/lib.sh"

OSD="${OSD_URL:-http://127.0.0.1:5601}"
osd() {  # METHOD PATH [curl args...]
  local m="$1" p="$2"; shift 2
  curl -sS --fail-with-body -m 60 -u "$OS_ADMIN_USER:$OS_ADMIN_PASSWORD" -X "$m" "$OSD$p" \
    -H 'osd-xsrf: true' -H 'securitytenant: global' "$@"
}

# The field list is what makes @timestamp and the keyword fields usable in visualizations; an
# index pattern saved without it fails with "Could not locate index-pattern-field".
fields="$(osd GET '/api/index_patterns/_fields_for_wildcard?pattern=containers-*&meta_fields=_source&meta_fields=_id&meta_fields=_index&meta_fields=_score' 2>/dev/null \
  | pyj 'print(json.dumps(json.dumps(d["fields"])))' || true)"
if [[ -z "$fields" ]]; then
  warn "no containers-* index yet: index pattern saved without fields; re-run once a stack ships"
  fields='"[]"'
fi
osd POST "/api/saved_objects/index-pattern/containers-all?overwrite=true" -H 'content-type: application/json' \
  -d "{\"attributes\":{\"title\":\"containers-*\",\"timeFieldName\":\"@timestamp\",\"fields\":$fields}}" >/dev/null
info "index pattern containers-* ($(pyj 'print(len(json.loads(d)))' <<<"$fields") fields)"

osd POST "/api/saved_objects/_import?overwrite=true" \
  --form "file=@$ORCA_LOGGING_DIR/dashboards/saved-objects/containers.ndjson" \
  | pyj "assert d['success'], d.get('errors'); print('    imported', d['successCount'], 'objects')"
info "Container Logs dashboard"
