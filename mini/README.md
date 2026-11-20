# Orcastra Mini

Orcastra Mini is the single-host edition of Orcastra CMP. The control plane, its Postgres, Redis
and a HashiCorp Vault run on one machine behind one nginx port, and operators sign in with TLS
client certificates instead of an identity provider. Every CMP feature is available except the
ones that need an identity provider or a marketplace federation: Settings > User Groups (roles are
set on the Operators page) and OrcaHub-synced organizations, owner keys and purchases. Integration
keys, add-ons and MCP work as in CMP.

The operator guide (install, upgrade, certificates, audit log, operations) is published at
<https://docs.orcastra.io> under Orcastra Mini.

## How the edition is built

This branch is CMP plus one edition package. CMP exposes seams (backend `app/editions/__init__.py`,
frontend `src/lib/edition.ts`, `src/lib/edition-auth.ts`, `src/components/edition-slots.tsx`,
`src/components/sidebar/edition-nav.ts`) and Mini fills them:

| Part | Where |
| --- | --- |
| Certificate sign-in, sessions bound to the certificate | `backend/app/editions/mini/{client_cert,tokens,verifier}.py`, `frontend/src/editions/mini/auth.ts` |
| Operators, roles, certificates, renewal | `backend/app/editions/mini/{identities,issuing}.py`, `routers/operators.py`, `frontend/src/editions/mini/operators/` |
| Hash-chained audit log in Postgres | `backend/app/editions/mini/audit.py`, `routers/audit_log.py`, `frontend/src/editions/mini/audit-log-view.tsx` |
| Directory and Migration Center authority without an IdP | `backend/app/editions/mini/{directory,authority}.py` |
| Bringing a Mini 1.0.0-RC1 database under Alembic | `backend/app/editions/mini/db_reconcile.py`, revisions `009`-`011`, `m001`, `m002` |
| Deployment | `docker-compose.mini.yml`, `mini/nginx/mini.conf`, `mini/vault/vault.hcl`, `.env.mini.example` |

`mini/VERSION` is the Mini release. The CMP release underneath is `frontend/package.json`, which
this branch never edits, and `backend/app/editions/mini/version.py` (`CMP_BASE`).

## Building and running from this tree

```bash
cp .env.mini.example .env.mini      # fill every empty value
make mini-init                       # templates into config/; add config/nginx/certs/server.{crt,key}
make mini-build                      # svlct/orcastra-dashboard-mini:{backend,frontend}-$(cat mini/VERSION)
make mini-up                         # MINI_PROJECT=orcastra-mini by default
```

## Keeping Mini current

Every CMP release is merged into this branch:

1. `git merge v<cmp-version>` (the CMP tag). Conflicts appear only where CMP changed a seam file;
   keep CMP's change and re-apply the one Mini line beside it.
2. If CMP added Alembic revisions, the graph now has two heads. Add
   `backend/alembic/versions/<date>_m00N_mini_merge_<cmp-head>.py` with
   `down_revision = ("<cmp head>", "<previous mini head>")` and no operations.
3. Set `CMP_BASE` in `backend/app/editions/mini/version.py` to the CMP release, and `mini/VERSION`
   to the Mini release that will ship.
4. Run the gates: `make test-backend` (includes the migration graph and the Mini tests),
   `make test-frontend` (includes `check-identity-seams` and `check-role-gates`),
   `make lint-backend lint-frontend`, and `make ci-local`.
5. Upgrade a copy of the previous Mini release's database and a fresh install in a lab, sign in
   with each role, and verify the audit chain before releasing.

A CMP change that reaches the identity provider directly fails `check-identity-seams` in both
lines: route it through the seam it bypassed.
