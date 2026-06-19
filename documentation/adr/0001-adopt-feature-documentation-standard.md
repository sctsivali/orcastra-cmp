---
status: Accepted
date: 2026-06-17
deciders: Maintainers (@daegal)
supersedes: none
superseded-by: none
---

# 0001. Adopt a versioned feature-documentation standard

## Context

Orcastra Dashboard had a strong top-level README and contributor docs, but no structured per-feature documentation. The reasoning behind a feature, its RBAC behavior, its configuration, and how to operate it lived only in code, commit messages, and the heads of the maintainers.

The product is non-trivial: a FastAPI backend and a Next.js frontend that control LXD/Incus clusters, with multi-tenant RBAC (Admin, Partner, Tenant), Authentik OIDC, Vault-managed certificates, and Alembic migrations. New contributors and operators need the reasoning and the operational steps, not only the code. This is also a public repository, so the documentation has to read as professional, human-authored engineering writing.

## Decision

Maintain engineering documentation as code, in a git-tracked root folder `documentation/`.

- One feature document per feature, grouped by product domain (`clusters/`, `instances/`, `networking/`, `storage/`, `auth/`, `rbac/`, `observability/`). Sections within a document separate reference, how-to, and explanation, without scattering one feature across four trees.
- A lean-adaptive template (`TEMPLATE.md`): core sections always present (Summary, Problem, Scope, How It Works, RBAC, Configuration, Security, Testing, Limitations, Revision History), with optional sections (Data Model, API, Runbook, Usage, diagrams) included only when relevant.
- ADRs in `adr/` for architectural decisions, immutable and superseded rather than rewritten.
- Documentation is required for behavior-changing work, written as the final step once the code is built, tested, and QA-passed, and checked during review.

## Alternatives Considered

- A single large design document or wiki: rejected because it drifts out of date and gets no per-change review.
- A full Diataxis split (tutorials, how-to, reference, explanation as top-level trees): rejected as heavier to maintain and prone to scattering one feature across four places, which is not worth it at this team size.
- A heavyweight template with all sections always present and "N/A" allowed: rejected as heavier per write and noisier; the lean-adaptive shape keeps the signal high.
- Documenting every change including pure refactors: rejected as noise; the trigger is behavior-changing work only.

## Consequences

Documentation is now versioned, reviewable, and stays current because it ships with the change. There is a single source of truth for behavior, rationale, and operations, which makes onboarding and operational bring-up far easier.

The cost is a real authoring step on every behavior-changing change, and the discipline to keep `updated` and `version` honest so documents do not drift.

The index and the domain folders grow over time. Existing shipped features are backfilled opportunistically.
