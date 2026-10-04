#!/usr/bin/env bash
# Apply ingest pipelines and index templates from the repo. Idempotent.
# Usage: scripts/apply-ingest-config.sh
source "$(dirname "$0")/lib.sh"

# Order matters: the *-ingest pipelines call the others.
for p in monthly-reroute vault-audit-parse vault-audit-ingest containers-normalize containers-ingest; do
  info "pipeline $p"
  os_json PUT "/_ingest/pipeline/$p" "$ORCA_LOGGING_DIR/opensearch/pipelines/$p.json" >/dev/null
done

for f in "$ORCA_LOGGING_DIR"/opensearch/templates/*.json; do
  name="$(basename "$f" .json)-template"
  info "index template $name"
  os_json PUT "/_index_template/$name" "$f" >/dev/null
done

# Simulate one daily name per template: proves routing + settings before real traffic hits them.
for f in "$ORCA_LOGGING_DIR"/opensearch/templates/*.json; do
  prefix="$(basename "$f" .json)"
  os POST "/_index_template/_simulate_index/$prefix-2099.01" | pyj "
s=d['template']['settings']['index']
assert s.get('number_of_replicas')=='0', s
# security-auditlog is the one prefix written under its final name, so it needs no routing pipeline
assert s.get('default_pipeline') or '$prefix' == 'security-auditlog', s
print('    simulate $prefix-2099.01 ok:', s.get('default_pipeline', 'no pipeline'))"
done
info "done"
