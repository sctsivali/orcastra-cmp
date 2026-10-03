# Isolated QA lab artifacts

## Development validation status

These artifacts accompany the hardening implementation and its regression fixtures.
It is not a production acceptance or deployment. Local evidence on the pre-sync
source records 15,338 backend passes, 14 separate PostgreSQL journal passes and a
scratch migration upgrade/downgrade/re-upgrade. The synthetic browser gate records
539 passes and 85 declared profile exclusions. The existing standalone frontend
also passed native musl Sharp/SWC, health, authentication redirect and WebP/AVIF
optimizer checks; that result does not prove a fresh Alpine Dockerfile build.

Real controller startup, SSO, LXD/Incus/MicroCloud behavior, per-role target-state
verification and remote resource cleanup remain open. The previously deferred
integration-role finding is unresolved. Keep these boundaries when using the
current development build. Private run manifests, credentials and runtime artifacts stay outside
the public repository.

## Lab execution contracts

These files prepare phased proposals and bounded lab executors. Importing a
module does not provision infrastructure. Execution requires approval for the
exact target, effect and reviewed proposal; a draft never grants authority.
Approval, host headroom, unverified topology, and cleanup conditions are recorded
in the untracked local manifest at `tasks/qa-hardening/LAB.md`.

- `outer_plan.py` renders a disconnected outer VM proposal offline. It requires a
  verified image fingerprint, per-run UUID, and selected registry alias for a
  complete payload. It has no execute mode, creates no bridge/ACL, and gives the
  VM no network device or inherited profile. Its fixed guest memory is 22 GiB;
  the fresh host preflight requires at least 30 GiB free to retain an 8 GiB reserve.
- `cloud-init.yaml` creates only private guest directories and ownership markers.
  It installs no package, configures no DNS, and imports no credential.
- `probe_nested_kvm.py` runs only through the approved VM's vsock agent, verifies
  both lab/run markers and absence of non-loopback interfaces, and probes KVM.
- `ubuntu-image-provenance.json` records the official Ubuntu 24.04 amd64 VM serial,
  fingerprint, file hashes/sizes, metadata digest, and exact LXD schema proof.
- `agent-provenance.json` records source evidence for LXD 5.21.8 vsock/config-drive
  support. It is not runtime evidence of a successful guest boot.
- `phase_a.py` is a narrow executor draft with no connection-discovery CLI. A
  caller must have fresh approval and supply the selected trusted client. It
  verifies daemon identity, architecture, RAM/storage, baseline absence, official
  image provenance, sole root disk, expanded configuration, and guest probe.
  Its private run ledger records stages, UTC timestamps, accepted operation IDs
  before waits, upload completion, and command return codes without outputs.
  Ambiguous writes stop without automatic retries or cleanup.
- `compose.lab.yaml` and `vault.hcl` are **Phase B proposals**. They cannot start
  without explicit image pins and fresh guest-private state. Application startup
  requires its explicit profile and fresh Vault/SSO configuration.
- `phase_b_pg_gate.py`, `phase_b_pg_runner.py`, and `phase_b_pg_guest.py` prepare
  a separate scratch journal and migration gate. A complete newly approved
  proposal must bind the successful B1 runtime, source inventory, sixty-five
  offline hash-locked wheels, two exact public images, and executor/runner hashes.
  The guest uses only its owned Unix daemon socket. Containers receive no socket,
  published port, outer NIC, shared data, or existing credential. A fresh internal
  bridge and two new database names remain confined to the owned guest.
  Guest cgroup, seccomp, AppArmor, capabilities and mount readbacks precede test
  execution. Accepted container/network IDs survive failures; cleanup is separate.
  The runner executes fourteen journal cases and a head/down023/head migration
  against a separate empty database. Allowlisted evidence includes test counts,
  fixture cleanup, and public column/default/constraint/index/sequence hashes.
  It does not prove restoration of customer data or sequence current values.
- `phase_b_pg_caller.py` renders an explicitly blocked transport preview.
  `phase_b_pg_loader.py` accepts only a separately sealed, closed USTAR stream
  with exact source, proposal and wrapper bindings. Its checked-in manifest is
  absent, so execution refuses before reading stdin, staging files or loading
  the SDK. A draft cannot manufacture B1 success or owner approval. Runtime
  sealing and the final private launcher require separate review.
- `phase_b_controller_stage.py` stages the fixed public Redis, Vault and
  Authentik blobs and verifies their archive/config/layer identities. Downloads
  remain opaque data: no image load, container, installation or guest transfer
  occurs. Fresh private staging, byte limits and disk reserves are mandatory.
- `phase_b_npm_stage.py` downloads only the fixed public lockfile tarballs and
  verifies SHA512 integrity, byte counts, and the outer archive. It does not
  unpack packages, populate an npm cache, install dependencies, select a Node
  runtime, or transfer data to a guest. One owned transport worker has bounded
  request supervision and shutdown; overall stage expiry is cooperative for
  filesystem operations and callbacks. Successful byte verification is a
  preparation checkpoint, not application-runtime acceptance.
- `phase_b_controller_plan.py` renders a pure B2 draft from explicit public pins
  and closed pre-execution proof references. Dependencies installed by B2,
  application builds and service acceptance are pending runtime postconditions,
  not circular prerequisites for their own initial execution approval. The draft
  remains unready and binds separate executor, transport and owner gates.
- `phase_b_vault_bootstrap.py` and `phase_b_idp_bootstrap.py` are bounded
  bootstrap libraries for a separately approved fresh controller phase. Their
  caller must prove exact container ownership, private state and transport.
  They refuse adoption, overwrite and retries. Vault initialization and private
  token/PKI material stay in protected guest files; the IdP contract covers six
  synthetic users and three role groups. Offline fixtures establish contracts,
  while actual response shapes, SSO login/JWT and revocation remain runtime gates.
- `*test*.py` checks identities, no-NIC constraints, proposal completeness,
  authority/capacity boundaries, exact cleanup targets, guest probe behavior, and
  partial failures using offline fakes.

Offline checks:

```bash
PATH=".venv/bin:$PATH" make lint-qa-artifacts test-qa-artifacts
python3 scripts/qa-hardening/outer_plan.py
```

The Makefile goals do not load local deployment environment files. CI runs the
same offline artifact gates in its Python job before database tests. Neither
goal provisions a guest, loads an image, or invokes a live target.

The completed approval preview also needs `--image-fingerprint`, `--run-id`,
`--cluster-alias`, and `--server-name`. The explicit server name binds the observed
daemon identity; a registry alias alone cannot complete the proposal. Its canonical SHA256 is
returned by `outer_plan.plan_digest(plan)`; approval names this exact digest and
target. The digest also binds the reviewed executor and public provenance hashes.
Do not infer approval from a nonempty reference or a syntactically valid hash.

Phase B downloads verified packages, OCI images, source, and browser dependencies
into dedicated controller task staging, then transfers bundles through the VM
agent. There is no guest Internet/NIC, shared host firewall mutation, DNS forwarder,
or outer network. All application/inner test networks exist inside the VM.
Review exact staging/installation/cleanup commands before running them.

Inside the approved guest, create credentials mode 0600 under
`/srv/orcastra-qa-hardening/state`, mode 0700. Review and pin OCI digests before
setting `QA_POSTGRES_IMAGE`, `QA_REDIS_IMAGE`, `QA_VAULT_IMAGE`, and
`QA_AUTHENTIK_IMAGE`. `QA_BACKEND_IMAGE` / `QA_FRONTEND_IMAGE` identify
locally built images with an explicit sanitized source inventory, commit/tree
digest, and existing Dockerfiles. `QA_STATE_DIR` is that exact private guest path.
Never reuse the repository `.env`, certificate directories, shared uploads, Docker
socket, host network, existing database, or production account.

| Private file | Required values, never printed |
| --- | --- |
| `app-database-password` | Fresh application PostgreSQL password |
| `auth-database-password` | Separate fresh Authentik PostgreSQL password |
| `authentik.env` | Matching Authentik DB password and fresh Authentik secret key |
| `authentik-bootstrap.env` | Synthetic admin email, fresh password hash and bootstrap API token |
| `backend.env` | App DB URL, app secret, Fernet key, restricted lab Vault token, lab SSO API token |
| `frontend.env` | Fresh Authentik client secret and NextAuth secret |

SSO client ID: `orcastra-qa-hardening`; redirect:
`http://127.0.0.1:32025/api/auth/callback/authentik`. The browser runs inside the
guest and resolves `auth.orcastra-qa.invalid` to loopback. Docker resolves the
same name to the new Authentik server alias. Verify JWT issuer/audience,
`role_admin`, `role_partner`, `role_tenant` claims and clock validity.
Synthetic Admin, Partner A/B, and Tenant A/B use `.invalid` identities.
Remove bootstrap tokens from worker configuration after creating the scoped lab
service identity. No token or real browser auth state belongs in source/evidence.

For later reviewed Compose validation, use an explicit nonsecret interpolation
file and `docker compose --env-file <lab-interpolation-file> -f compose.lab.yaml
config --quiet`. Do not print full resolved Compose config, container environments,
Vault initialize responses, OIDC responses, or browser auth state.

Each runtime gate needs separate captured evidence: guest boot/KVM, dependency
startup, fresh Vault PKI, SSO login/logout/claims, migrations/readiness, owned
targets, feature assertions, cleanup, and retained image capacity. The private
local manifest records completed checkpoints and remaining gates. Passing offline
artifact checks is not passing live QA or authorization to execute a draft.
