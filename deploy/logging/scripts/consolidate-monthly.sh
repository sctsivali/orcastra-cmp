#!/usr/bin/env bash
# Merge daily indices (<prefix>-YYYY.MM.DD) into monthly indices (<prefix>-YYYY.MM).
#
# Usage: scripts/consolidate-monthly.sh [--apply] [--delete --snapshot <name>] [--include-today] <prefix>...
#   default    dry run: prints the plan only
#   --apply    route existing daily indices to the monthly pipeline, write-block them, reindex
#   --delete   delete a daily index only after its reindex accounted for every document AND it
#              is contained in the given snapshot
#   --include-today  also take today's index of a prefix without a routing pipeline; only once its
#              writer has moved to monthly names (security-auditlog after the opensearch.yml change)
#
# Safe to re-run: reindex uses op_type=create with the source _id, so documents already copied
# (or written live into the monthly index) are counted as version conflicts, never duplicated.
source "$(dirname "$0")/lib.sh"

apply=0; delete=0; snap=""; include_today=0; prefixes=()
while (($#)); do
  case "$1" in
    --apply) apply=1 ;;
    --delete) delete=1 ;;
    --include-today) include_today=1 ;;
    --snapshot) snap="${2:?}"; shift ;;
    -*) die "unknown flag $1" ;;
    *) prefixes+=("$1") ;;
  esac; shift
done
((${#prefixes[@]})) || die "at least one prefix required"
((delete == 0 || apply == 1)) || die "--delete requires --apply"
((delete == 0)) || [[ -n "$snap" ]] || die "--delete requires --snapshot <name>"

snap_indices=""
if [[ -n "$snap" ]]; then
  snap_indices="$(os GET "/_snapshot/${SNAPSHOT_REPO:-orcastra-archive}/$snap" | pyj "
s=d['snapshots'][0]; assert s['state']=='SUCCESS', s['state']; print(' '.join(s['indices']))")"
fi

for prefix in "${prefixes[@]}"; do
  # Daily index names only; the monthly target itself is never a source.
  mapfile -t daily < <(os GET "/_cat/indices/$prefix-*?h=index&s=index" | grep -E "^${prefix//./\\.}-[0-9]{4}\.[0-9]{2}\.[0-9]{2}$" || true)
  info "$prefix: ${#daily[@]} daily indices"
  ((${#daily[@]})) || continue

  # security-auditlog is written by the security plugin under its own name pattern, so it has no
  # routing pipeline; its monthly naming comes from opensearch.yml (plugins.security.audit.config.index).
  case "$prefix" in
    vault-audit) pipeline="vault-audit-ingest" ;;
    security-auditlog) pipeline="" ;;
    *) pipeline="monthly-reroute" ;;
  esac
  if ((apply)); then
    # Late writes addressed to an existing daily name now land in the monthly index too.
    if [[ -n "$pipeline" ]]; then
      os PUT "/$prefix-*/_settings" -H 'content-type: application/json' \
        -d "{\"index.default_pipeline\":\"$pipeline\"}" >/dev/null
    fi
  fi

  today="$(date -u +%Y.%m.%d)"
  for idx in "${daily[@]}"; do
    month="${idx%.*}"
    # Without a routing pipeline today's index is still written by name; blocking it would fail those writes.
    if [[ -z "$pipeline" && $include_today == 0 && "$idx" == *"-$today" ]]; then
      echo "    skip $idx (still written directly today)"; continue
    fi
    src_count="$(os GET "/$idx/_count" | pyj 'print(d["count"])')"
    if ((apply == 0)); then echo "    plan: $idx ($src_count docs) -> $month"; continue; fi

    os PUT "/$idx/_settings" -H 'content-type: application/json' -d '{"index.blocks.write":true}' >/dev/null
    if [[ "$src_count" == 0 ]]; then
      # Nothing to copy; do not create an empty monthly index just to hold it.
      echo "    $idx: empty"
      if ((delete)); then
        [[ " $snap_indices " == *" $idx "* ]] || die "$idx is not in snapshot $snap; refusing to delete"
        os DELETE "/$idx" >/dev/null; echo "    deleted $idx"
      fi
      continue
    fi
    # Create the monthly index through its template (settings/mappings) if it does not exist yet.
    os_exists "/$month" || os PUT "/$month" >/dev/null
    # note: dest.pipeline=_none so vault documents are not parsed twice; they were parsed on ingest.
    res="$(OS_TIMEOUT=1800 os POST "/_reindex?wait_for_completion=true&refresh=true" -H 'content-type: application/json' -d "{
      \"conflicts\":\"proceed\",
      \"source\":{\"index\":\"$idx\",\"size\":5000},
      \"dest\":{\"index\":\"$month\",\"op_type\":\"create\",\"pipeline\":\"_none\"}}")"
    read -r total created conflicts failures < <(echo "$res" | pyj 'print(d["total"], d["created"], d["version_conflicts"], len(d["failures"]))')
    status="ok"
    if [[ "$total" != "$src_count" || $((created + conflicts)) -ne "$total" || "$failures" != 0 ]]; then
      status="MISMATCH"
    fi
    echo "    $idx -> $month: source=$src_count total=$total created=$created already_present=$conflicts failures=$failures [$status]"
    [[ "$status" == ok ]] || die "accounting mismatch on $idx; daily index kept (write-blocked)"

    if ((delete)); then
      [[ " $snap_indices " == *" $idx "* ]] || die "$idx is not in snapshot $snap; refusing to delete"
      os DELETE "/$idx" >/dev/null
      echo "    deleted $idx"
    fi
  done
done
info "done"
