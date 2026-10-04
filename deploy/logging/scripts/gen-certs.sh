#!/usr/bin/env bash
# Generate the logging host PKI: a private CA, the node certificate and the security admin cert.
# Runs ON the logging host. Replaces the OpenSearch demo certificates (whose keys are public).
#
# Usage: scripts/gen-certs.sh            create anything missing (never overwrites)
#        scripts/gen-certs.sh --renew    re-issue node + admin certs from the existing CA
#
# Output in $ORCA_LOGGING_DIR/certs:
#   root-ca.pem (public, give to clients that verify)   root-ca-key.pem (root only, back up offline)
#   node.pem / node-key.pem (mounted into OpenSearch, owned by uid 1000)
#   admin.pem / admin-key.pem (root only; used by apply-security.sh, never mounted)
set -euo pipefail

DIR="${ORCA_LOGGING_DIR:-$(cd "$(dirname "$0")/.." && pwd)}/certs"
OS_UID=1000   # opensearch user inside the official image
CA_DAYS=3650
LEAF_DAYS=825
# SANs: compose service name (Dashboards, in-network clients), loopback, host names and both host
# addresses that clients use (private LAN for Orcastra/Vault shippers, Tailscale for other stacks).
# logs.orcastra.io: the Dashboards security plugin forwards the browser's Host header on some calls
# to OpenSearch (readonly-tenant check), so Node verifies the node cert against the public name.
NODE_SAN="DNS:opensearch,DNS:localhost,DNS:prod-orcastra-logging,DNS:logs.orcastra.io,IP:127.0.0.1,IP:10.1.1.40,IP:100.90.90.69"

renew=0; [[ "${1:-}" == "--renew" ]] && renew=1
mkdir -p "$DIR"; chmod 750 "$DIR"; cd "$DIR"
umask 077

if [[ ! -s root-ca-key.pem ]]; then
  ((renew == 0)) || { echo "ERROR: --renew needs an existing CA" >&2; exit 1; }
  openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out root-ca-key.pem
  openssl req -x509 -new -key root-ca-key.pem -sha256 -days "$CA_DAYS" -out root-ca.pem \
    -subj "/O=Orcastra/OU=logging/CN=Orcastra Logging Root CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "created CA"
fi

issue() {  # name subject extfile-content
  local name="$1" subj="$2" ext="$3"
  if [[ -s "$name.pem" && $renew == 0 ]]; then echo "$name exists (use --renew to re-issue)"; return; fi
  # OpenSearch requires PKCS#8 keys; genpkey writes PKCS#8.
  openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$name-key.pem.new"
  openssl req -new -key "$name-key.pem.new" -subj "$subj" -out "$name.csr"
  printf '%s\n' "$ext" > "$name.ext"
  openssl x509 -req -in "$name.csr" -CA root-ca.pem -CAkey root-ca-key.pem -CAcreateserial \
    -sha256 -days "$LEAF_DAYS" -extfile "$name.ext" -out "$name.pem.new"
  openssl verify -CAfile root-ca.pem "$name.pem.new" >/dev/null
  mv "$name-key.pem.new" "$name-key.pem"; mv "$name.pem.new" "$name.pem"; rm -f "$name.csr" "$name.ext"
  echo "issued $name: $(openssl x509 -in "$name.pem" -noout -subject -enddate | tr '\n' ' ')"
}

issue node "/O=Orcastra/OU=logging/CN=opensearch-node1" \
"basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=$NODE_SAN"

issue admin "/O=Orcastra/OU=logging/CN=orcastra-logging-admin" \
"basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth"

# The container reads node cert/key and the CA; nothing else is mounted.
chown root:root root-ca-key.pem admin.pem admin-key.pem root-ca.srl 2>/dev/null || true
chmod 600 root-ca-key.pem admin-key.pem
chmod 644 root-ca.pem admin.pem
chown "$OS_UID:$OS_UID" node.pem node-key.pem
chmod 600 node-key.pem; chmod 644 node.pem
ls -l "$DIR"
