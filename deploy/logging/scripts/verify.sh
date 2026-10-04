#!/usr/bin/env bash
# Health and safety checks for the logging host. Runs ON the host. Exit 1 if any check fails.
# Usage: scripts/verify.sh [--max-lag-min N]
source "$(dirname "$0")/lib.sh"
set +e

max_lag=15; [[ "${1:-}" == "--max-lag-min" ]] && max_lag="${2:?}"
fail=0
ok()  { echo "  PASS $*"; }
bad() { echo "  FAIL $*"; fail=1; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

echo "== cluster"
h="$(os GET /_cluster/health)" || bad "cluster reachable"
status="$(pyj 'print(d["status"])' <<<"$h")"
[[ "$status" == green ]] && ok "status green" || bad "status $status"
shards="$(pyj 'print(d["active_shards"]+d["unassigned_shards"])' <<<"$h")"
limit="$(os GET '/_cluster/settings?include_defaults=true&flat_settings=true' | pyj 'm={**d["defaults"],**d["persistent"],**d["transient"]}; print(m["cluster.max_shards_per_node"])')"
(( shards * 100 / limit < 50 )) && ok "shards $shards / $limit" || bad "shards $shards / $limit (>= 50%)"
disk="$(os GET '/_cat/allocation?h=disk.percent' | head -1 | tr -d ' ')"
(( disk < 75 )) && ok "disk ${disk}%" || bad "disk ${disk}% (>= 75%, watermark low is 85%)"
heap="$(os GET /_nodes/stats/jvm | pyj 'print(list(d["nodes"].values())[0]["jvm"]["mem"]["heap_used_percent"])')"
(( heap < 85 )) && ok "heap ${heap}%" || bad "heap ${heap}%"
daily="$(os GET '/_cat/indices?h=index' | grep -cE '^(orcastra-(access|audit|app)|vault-audit|containers-.+|security-auditlog)-[0-9]{4}\.[0-9]{2}\.[0-9]{2}$')"
[[ "$daily" == 0 ]] && ok "no daily log indices" || bad "$daily daily log indices exist (routing or audit index config not applied)"

echo "== TLS"
if [[ -r "$OS_CACERT" ]]; then
  echo | openssl s_client -connect localhost:9200 -CAfile "$OS_CACERT" -verify_return_error -servername opensearch >/dev/null 2>&1 \
    && ok "node cert verifies against our CA" || bad "node cert does not verify against $OS_CACERT"
  issuer="$(echo | openssl s_client -connect localhost:9200 2>/dev/null | openssl x509 -noout -issuer)"
  [[ "$issuer" != *"Example Com"* ]] && ok "not the demo CA" || bad "demo certificate in use"
  for c in node admin root-ca; do
    f="$ORCA_LOGGING_DIR/certs/$c.pem"; [[ -r "$f" ]] || continue
    openssl x509 -in "$f" -noout -checkend $((30*86400)) >/dev/null && ok "$c.pem valid > 30d" || bad "$c.pem expires within 30 days"
  done
else
  bad "CA file $OS_CACERT missing (demo certs?)"
fi

echo "== ingest freshness (max ${max_lag}m)"
for p in orcastra-access orcastra-audit orcastra-app ${VERIFY_EXTRA_PREFIXES:-}; do
  lag="$(os GET "/$p-*/_search?size=0" -H 'content-type: application/json' \
    -d '{"aggs":{"m":{"max":{"field":"@timestamp"}}}}' | pyj '
import datetime as t
v=d["aggregations"]["m"]["value"]
print(int((t.datetime.now(t.timezone.utc).timestamp()*1000-v)/60000) if v else 99999)')"
  (( lag <= max_lag )) && ok "$p latest ${lag}m ago" || bad "$p latest ${lag}m ago"
done

echo "== security config"
cfg="$(os GET /_plugins/_security/api/securityconfig)"
pyj 'a=d["config"]["dynamic"]["authc"]; assert a["openid_auth_domain"]["http_enabled"] and a["basic_internal_auth_domain"]["http_enabled"]' <<<"$cfg" 2>/dev/null \
  && ok "basic + openid auth domains enabled" || bad "auth domains not as expected"
for u in admin kibanaserver fluentbit; do
  os GET "/_plugins/_security/api/internalusers/$u" >/dev/null 2>&1 && ok "internal user $u present" || bad "internal user $u missing"
done

echo "== exposure"
ss -Htln | awk '{print $4}' | grep -qE ':9300$' && bad "9300 listening on the host" || ok "9300 not published"
ss -Htln | awk '{print $4}' | grep -E ':5601$' | grep -qvE '^(10\.1\.1\.40|127\.0\.0\.1):5601$' && bad "5601 bound beyond LAN/loopback" || ok "5601 only on 10.1.1.40 and loopback"

echo "== dashboards"
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status)"
[[ "$code" == 401 || "$code" == 200 ]] && ok "dashboards serving ($code)" || bad "dashboards http $code"
loc="$(curl -s -o /dev/null -w '%{redirect_url}' 'http://127.0.0.1:5601/auth/openid/login?nextUrl=%2F')"
[[ "$loc" == https://sso.orcastra.io/application/o/authorize/* && "$loc" == *"redirect_uri=https%3A%2F%2Flogs.orcastra.io%2Fauth%2Fopenid%2Flogin"* ]] \
  && ok "OIDC login redirects to Authentik with the public redirect_uri" || bad "OIDC redirect unexpected: ${loc:0:120}"

echo
((fail == 0)) && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit "$fail"
