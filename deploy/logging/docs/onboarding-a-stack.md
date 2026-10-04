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

## 2. Stack host: Fluent Bit output

Any Fluent Bit (>= 2.x) that receives the containers' logs works. Docker's `fluentd` log driver
pointing at a local Fluent Bit `forward` input is the reference setup (OrcaHub's
`docker-compose.observability.yml`); tailing `/var/lib/docker/containers` also works but loses the
container name unless you add it yourself.

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

Docker's `fluentd` driver (and `json-file`) split lines longer than 16 KB into several records,
and multi-line output (stack traces) arrives one line per record; the aggregator stores them as
separate documents. Log single-line JSON from applications where possible.

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

In Dashboards create (or import) an index pattern `containers-*` with time field `@timestamp`;
filter with `stack`, `env`, `container_name`, `level`.
