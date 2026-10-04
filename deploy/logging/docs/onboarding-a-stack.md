# Onboarding a stack's container logs

How to ship every Docker container of a stack (OrcaHub, a future service, ...) to the central
logging host. Example values: stack `orcahub`, environment `prod`.

## 1. Logging host: writer account

Add a write-only role and its mapping, then apply:

```yaml
# opensearch/security/roles.yml
writer_orcahub_prod:
  reserved: false
  cluster_permissions: ["indices:data/write/bulk"]
  index_permissions:
    - index_patterns: ["containers-orcahub-prod-*"]
      allowed_actions: ["create_index", "write"]

# opensearch/security/roles_mapping.yml
writer_orcahub_prod:
  reserved: false
  backend_roles: ["writer_orcahub_prod"]
```

```bash
scripts/apply-security.sh
scripts/provision-stack.sh orcahub prod      # creates fluentbit-orcahub-prod, password in secrets/
```

The account can create and write `containers-orcahub-prod-*` only. It cannot read, delete, or
touch another stack's or environment's indices (verified: every such request is 403).

## 2. Stack host: ship the logs

### Option A (recommended): the generic shipper

`shipper/` is a self-contained Fluent Bit that tails every `json-file` container log on the host
and adds the container name, compose project/service and image from Docker's own metadata. The
application containers are not touched or restarted. Lines Docker split at 16 KB are joined again,
and read offsets survive restarts.

```bash
# on the stack host, as root
install -d -m 700 /opt/log-shipper
# copy shipper/{docker-compose.yml,fluent-bit.conf,parsers.conf,docker-metadata.lua} there,
# and certs/root-ca.pem as /opt/log-shipper/ca.pem
cp .env.example .env && chmod 600 .env     # fill in host, user, password, stack, env, host name
docker compose -f /opt/log-shipper/docker-compose.yml up -d
```

Existing history is not re-shipped: the shipper starts at the end of each log file. Containers
using another log driver (for example `fluentd`) have no `json-file` log and are not picked up;
ship those with option B.

### Option B: your own Fluent Bit

Any Fluent Bit (>= 2.x) that already receives the containers' logs can add an output. OrcaHub does
this: its overlay gives every service the `fluentd` driver and its Fluent Bit adds
`outputs.d/opensearch.conf`. The output section looks like this:

```ini
[OUTPUT]
    name              opensearch
    match             <your tag pattern>
    host              100.90.90.69          # Tailscale; 10.1.1.40 from the private LAN
    port              9200
    tls               on
    tls.verify        on
    tls.ca_file       /path/to/orcastra-logging-ca.pem   # certs/root-ca.pem from this repo
    http_user         fluentbit-orcahub-prod
    http_passwd       <secrets/fluentbit-orcahub-prod.password>
    logstash_format   on
    logstash_prefix   containers-orcahub-prod
    logstash_dateformat %Y.%m.%d
    suppress_type_name on
    generate_id       on                    # retries overwrite instead of duplicating
    retry_limit       no_limits
    storage.total_limit_size 512M
```

Copy the password over SSH, never through chat or tickets:

```bash
ssh root@100.90.90.69 cat /opt/orcastra-logging/secrets/fluentbit-orcahub-prod.password \
  | ssh <stack-host> 'umask 077; cat > /tmp/pw'   # then place it into the output config, chmod 600
```

With option B, Docker splits lines longer than 16 KB into several records (option A joins them).
With either option, multi-line output (stack traces) arrives one line per record. Log single-line
JSON from applications where possible.

The aggregator parses JSON log lines itself (`message`/`msg`, `level`/`severity`, `request_id`
are lifted; everything else lands in `fields.*`) and recognises the level markers of common
plain-text formats (`[ERROR`, ` FATAL:`, `level=warn`, ...).

Optional record fields the aggregator understands: `container_name`, `container_id`, `source`,
`level`, `message`, `request_id`/`requestId`, `host`, `log`. `stack` and `env` are derived from
the index name. Anything else goes to `fields.*`.

## 3. Verify

```bash
# logging host
source scripts/lib.sh
os GET '/containers-orcahub-prod-*/_search?size=0' -H 'content-type: application/json' \
  -d '{"aggs":{"c":{"terms":{"field":"container_name","size":50}}}}'
VERIFY_EXTRA_PREFIXES=containers-orcahub-prod scripts/verify.sh
```

Open the **Container Logs** dashboard (Global tenant) and pick the stack in its filters. After the
first stack ships, run `scripts/dashboards-objects.sh` once so the `containers-*` index pattern
picks up the field list.
