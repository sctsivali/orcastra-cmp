#!/usr/bin/env bash
# Create the shared Dashboards objects (Global tenant). Idempotent: fixed IDs + overwrite.
# Runs ON the host against the local Dashboards (break-glass admin, basic auth).
#   containers-*   index pattern for every stack's container logs
#   "Container errors" saved search
# The Orcastra dashboards and their index patterns (orcastra-*, vault-audit-*) come from
# orcastra-cmp's setup_opensearch_dashboards.sh / ndjson exports; they must live in the Global
# tenant too, or SSO users (who land in Global) do not see them.
source "$(dirname "$0")/lib.sh"

OSD="${OSD_URL:-http://127.0.0.1:5601}"
osd() {  # METHOD PATH [json]
  curl -sS --fail-with-body -m 30 -u "$OS_ADMIN_USER:$OS_ADMIN_PASSWORD" -X "$1" "$OSD$2" \
    -H 'osd-xsrf: true' -H 'securitytenant: global' -H 'content-type: application/json' ${3:+-d "$3"}
}

pattern() {  # id title
  osd POST "/api/saved_objects/index-pattern/$1?overwrite=true" \
    "{\"attributes\":{\"title\":\"$2\",\"timeFieldName\":\"@timestamp\"}}" >/dev/null
  info "index pattern $2 ($1)"
}
pattern containers-all "containers-*"

osd POST "/api/saved_objects/search/containers-errors?overwrite=true" '{
  "attributes": {
    "title": "Container errors",
    "description": "Error-level lines from every stack (containers-*)",
    "columns": ["stack","env","container_name","level","message"],
    "sort": [["@timestamp","desc"]],
    "kibanaSavedObjectMeta": {"searchSourceJSON": "{\"query\":{\"language\":\"kuery\",\"query\":\"level:(error or fatal or ERROR or FATAL or 50 or 60) or message:*ERROR* or message:*FATAL*\"},\"filter\":[],\"indexRefName\":\"kibanaSavedObjectMeta.searchSourceJSON.index\"}"}
  },
  "references": [{"name":"kibanaSavedObjectMeta.searchSourceJSON.index","type":"index-pattern","id":"containers-all"}]
}' >/dev/null
info "saved search Container errors"

osd GET "/api/saved_objects/_find?type=index-pattern&per_page=100" | pyj "print('    index patterns:', sorted(o['attributes']['title'] for o in d['saved_objects']))"
