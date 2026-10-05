# Logging host runbook

Operating `prod-orcastra-logging` (OpenSearch + OpenSearch Dashboards, single node), the central
log store behind `https://logs.orcastra.io`.

## Layout

| Item | Value |
|---|---|
| Host | `prod-orcastra-logging`, LAN `10.1.1.40`, Tailscale `100.90.90.69` |
| Deploy dir | `/opt/orcastra-logging` (this directory, synced from git; root only) |
| Compose project | `orcastra-logging` (`compose/docker-compose.yml`) |
| Data volumes | `root_opensearch-data`, `root_opensearch-snapshots` (bind to `/opt/opensearch/archive`). The names come from the original `/root` compose project and are kept on purpose. |
| Versions | OpenSearch and Dashboards 3.5.0, pinned by digest |
| Public URL | `https://logs.orcastra.io`: Cloudflare Tunnel (`cloudflared` on `10.1.1.50`) to `http://10.1.1.40:5601` |
| Ingest | `https://<LAN or Tailscale IP>:9200`, TLS from our own CA (`certs/root-ca.pem`) |
| Own container logs | `/opt/log-shipper` (the generic shipper, stack `logging`, env `prod`) |

Files that never go to git (see `.gitignore`):

| Path | Content |
|---|---|
| `compose/.env` | `OPENSEARCH_ADMIN_PASSWORD` (break-glass admin), read by the scripts |
| `certs/root-ca-key.pem` | CA private key. Keep an offline copy; losing it means re-issuing every cert. |
| `certs/admin*.pem` | security admin client cert (securityadmin only, never mounted) |
| `certs/node*.pem` | node cert/key (mounted read-only into OpenSearch) |
| `secrets/kibanaserver_password`, `secrets/opensearch_oidc_client_secret`, `secrets/cookie_password` | inputs of the Dashboards keystore |
| `secrets/opensearch_dashboards.keystore` | built by `scripts/dashboards-keystore.sh` |
| `secrets/fluentbit-<stack>-<env>.password` | writer passwords from `scripts/provision-stack.sh` |

## Hosts shipping container logs

| Stack/env | Host | How | Notes |
|---|---|---|---|
| `logging/prod` | prod-orcastra-logging (this host) | generic shipper `/opt/log-shipper` | OpenSearch and Dashboards' own logs |
| `orcastra/prod` | prod-orcastra-dashboard (`10.1.1.50`, `100.126.140.57`) | generic shipper `/opt/log-shipper` | all Orcastra containers (json-file) |
| `orcahub/prod` | c2-prod-orcahub (`100.104.230.76`, central2) | generic shipper `/opt/log-shipper` | json-file containers today (postgres, redis, pg-backup, monitoring). backend/frontend use the fluentd driver and arrive through OrcaHub's own fluent-bit once a release containing its `outputs.d/opensearch.conf` support is deployed; that file is already in place on the host |

`scripts/verify.sh` fails when any of them stops shipping for more than 30 minutes.

## Index layout

All log indices are **monthly** (`<prefix>-YYYY.MM`). Shippers keep sending Logstash-style daily
names (`<prefix>-YYYY.MM.DD`); the `default_pipeline` of each index template rewrites the target
to the monthly index (`opensearch/pipelines/monthly-reroute.json`), so no shipper needs to know.
This keeps the single node far below `cluster.max_shards_per_node` (1000): before this change
daily indices had reached 909 shards.

| Prefix | Writer | Template |
|---|---|---|
| `orcastra-access`, `orcastra-audit`, `orcastra-app` | Orcastra dashboard Fluent Bit (`fluentbit`, role `log_writer`) | `opensearch/templates/orcastra-*.json` |
| `vault-audit` | Vault host Fluent Bit (`fluentbit`) | `vault-audit.json` (pipeline `vault-audit-ingest`) |
| `containers-<stack>-<env>` | per-stack Fluent Bit (`fluentbit-<stack>-<env>`, write-only) | `containers.json` (pipeline `containers-ingest`) |
| `security-auditlog` | OpenSearch security plugin | monthly via `plugins.security.audit.config.index` |

`containers-*` documents keep a fixed set of fields (`@timestamp stack env host container_name
container_id source level message request_id log`); everything else an application logs as JSON
lands in the `flat_object` field `fields`, searchable as `fields.<key>:<value>`. This prevents
mapping explosion and type conflicts (a rejected document would make the shipper retry forever).

## Routine operations

All commands run on the host as root from `/opt/orcastra-logging`.

```bash
scripts/verify.sh                      # health, TLS, shards, freshness, exposure, OIDC redirect
scripts/apply-ingest-config.sh         # pipelines + index templates (idempotent)
scripts/apply-security.sh              # config/roles/rolesmapping, backs up first
scripts/snapshot.sh <lowercase-name>   # snapshot into the orcastra-archive repository
```

Deploying a change from git (operator machine):

```bash
rsync -a --exclude .git --exclude certs/ --exclude secrets/ --exclude compose/.env \
  deploy/logging/ root@100.90.90.69:/opt/orcastra-logging/
```

Then run whichever apply script the change needs, or `docker compose -f compose/docker-compose.yml up -d`
for compose / yml changes, and finish with `scripts/verify.sh`.

## Access control

- People sign in with **Authentik** (`sso.orcastra.io`, application `opensearch-dashboards`).
  Group `opensearch-admins` maps to `all_access`; group `opensearch-viewers` maps to `readall`
  plus read-only Dashboards. Membership changes take effect on the next sign-in or token refresh
  (access token lifetime 10 minutes). Users outside both groups are refused by Authentik.
- Tokens are bound to this provider (`required_issuer`, `required_audience` in
  `opensearch/security/config.yml`); tokens minted for other Authentik apps are rejected even
  though Authentik signs every app with the same key.
- The internal `admin` user is the **break-glass** account (basic auth, still offered on the
  login page). Service accounts: `kibanaserver`, `fluentbit` (Orcastra/Vault), `audit_viewer`,
  `fluentbit-<stack>-<env>`.
- Re-running the Authentik side: `scripts/authentik-provision.py` (idempotent; `--delete` to remove).
- Shared dashboards, index patterns and saved searches live in the **Global** tenant, which SSO
  users land in. Objects saved in someone's Private tenant are visible only to that person.
  (Until 2026-10-04 the Orcastra dashboards sat in the `admin` user's Private tenant; they were
  copied to Global during the migration, export kept in `/root/backups/cutover-*/`.)

### Authentik down

Dashboards fetches the OIDC discovery document at startup and **refuses to start** if it cannot.
A running Dashboards is unaffected by an Authentik outage, but a restart during one takes the UI
down, basic-auth login included. Use the break-glass instance:

```bash
docker compose -f compose/docker-compose.yml --profile breakglass up -d opensearch-dashboards-breakglass
# from your machine:
ssh -L 5602:127.0.0.1:5602 root@100.90.90.69   # then open http://localhost:5602, log in as admin
docker compose -f compose/docker-compose.yml --profile breakglass rm -sf opensearch-dashboards-breakglass   # afterwards
```

The API (`curl -u admin ... https://localhost:9200`) never depends on Authentik.

## Certificates

`scripts/gen-certs.sh` created a private CA (10 years) and node/admin certs (825 days). Renew the
leaf certs before expiry (`scripts/verify.sh` fails 30 days ahead):

```bash
scripts/gen-certs.sh --renew
docker compose -f compose/docker-compose.yml up -d --force-recreate opensearch opensearch-dashboards
scripts/verify.sh
```

Node SANs: `opensearch`, `localhost`, `prod-orcastra-logging`, `logs.orcastra.io`, `127.0.0.1`,
`10.1.1.40`, `100.90.90.69`. `logs.orcastra.io` is needed because the Dashboards security plugin
forwards the browser's Host header on its read-only tenant check, so that call verifies the node
certificate against the public name. A new client address needs a SAN added in `gen-certs.sh` and a renewal.
Clients that verify TLS get `certs/root-ca.pem` (public).

## Snapshots and restore

The fs repository `orcastra-archive` lives on the same disk (`/opt/opensearch/archive`). It
protects against mistakes and bad migrations, not against losing the VM; copy it off-host for
disaster recovery. Restore one index under a new name:

```bash
source scripts/lib.sh
os POST "/_snapshot/orcastra-archive/<snapshot>/_restore?wait_for_completion=true" \
  -H 'content-type: application/json' \
  -d '{"indices":"<index>","rename_pattern":"(.+)","rename_replacement":"restored-$1"}'
```

## Rollback

| Change | Rollback |
|---|---|
| Security config | `scripts/apply-security.sh --restore /root/backups/<ts>/security` (every apply writes one) |
| Compose / certs / yml | `git` revert, re-sync, `docker compose up -d`; previous files are in `/root/backups/<ts>/` |
| Authentik client | `scripts/authentik-provision.py --delete` |
| Pipelines / templates | re-apply the previous git version with `apply-ingest-config.sh` |

## Known limitations

- Docker splits a log line longer than 16 KB into several records; each part is stored as its own
  document (same container, consecutive timestamps). Multi-line output such as stack traces
  arrives one line per document.
- The first page load of `logs.orcastra.io` can take 20 seconds or more while the Dashboards
  bundles come through Cloudflare. The login page shows the break-glass form first and the
  "Log in with Orcastra SSO" button below it; Dashboards does not allow reordering them.

- Single node: no replica, so a lost disk loses the data since the last off-host snapshot copy.
- No retention (ISM) policy is attached yet: indices grow until one is agreed and applied.
- The `log_writer` role (Orcastra/Vault shipper) still carries `manage` on its indices, which
  lets that account delete them. Tighten once the shippers are confirmed to need only write.
- Public basic-auth login has no rate limit inside OpenSearch (a username lockout would let anyone
  lock out the shipper accounts). Rate-limit `POST /auth/login` at Cloudflare.
