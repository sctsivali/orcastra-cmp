# Orcastra Dashboard Engineering Documentation

Versioned documentation for Orcastra Dashboard features and architecture. This is the source of truth for how features work, why they were built the way they were, and how to operate them. It is maintained as code: every behavior-changing change updates the relevant document in the same commit.

> Audience: Orcastra engineers and operators. English, technical, kept current.

---

## 1. Principles

1. Documentation lives in the repo, is git-tracked, and changes in the same commit as the behavior it describes. It is never an afterthought.
2. Each section serves one reader intent: reference (the facts of an API, schema, or config), how-to (operating it), and explanation (why it is built this way). Keep those intents separate within a document.
3. Do not duplicate content. Cross-link to ADRs and related documents rather than copying.
4. State who a document is for. A runbook for operators and a design rationale for engineers are different readers.
5. Be honest about limitations, deferred phases, and sharp edges. A document that hides them is worse than none.
6. Record architectural decisions in an ADR (`adr/`). An ADR is immutable and is superseded rather than rewritten.

## 2. Structure

```
documentation/
├── README.md                 # this file: the standard and the index
├── TEMPLATE.md               # feature document template (copy this)
├── adr/                      # Architecture Decision Records
│   ├── README.md
│   ├── TEMPLATE.md
│   └── NNNN-short-title.md
├── clusters/                 # multi-cluster, registration, members, regions
├── instances/                # instance lifecycle, console/terminal, images, profiles
├── networking/               # virtual networks, ACLs, IPAM
├── storage/                  # storage pools and volumes
├── auth/                     # Authentik OIDC, JWT, sessions, API keys
├── rbac/                     # roles, permissions, tenant access policies
├── observability/            # logging (Fluent Bit, OpenSearch), monitoring, health
└── ui/                       # cross-cutting frontend UX (shared table behaviors)
```

One feature per file, grouped by domain folder. Create a domain folder on first use; do not pre-create empty folders. Domains map to the product, not to the code layout, so a feature that spans backend and frontend still gets one document in its product domain.

## 3. Document types

| Type | Lives in | Purpose |
|------|----------|---------|
| Feature doc | `<domain>/<feature>.md` | How a shipped feature works end to end, how to operate it, how it was tested. Uses `TEMPLATE.md`. |
| ADR | `adr/NNNN-*.md` | One architectural decision and its consequences. Immutable. Uses `adr/TEMPLATE.md`. |

A feature doc explains the feature. An ADR explains one decision, which many feature docs may reference.

## 4. Naming and metadata

File names are `kebab-case`, descriptive, and stable, for example `cluster-registration.md` or `instance-console.md`. Renaming breaks links, so supersede rather than rename. Every document opens with the YAML frontmatter from the template (`title`, `status`, `owner`, `domain`, `version`, `created`, `updated`, `related`, `audience`). Keep `updated` accurate on every edit.

### Status lifecycle

| Status | Meaning |
|--------|---------|
| `Draft` | Being written; not yet trustworthy. |
| `Active` | Current and accurate. The default for a shipped feature. |
| `Deprecated` | Still exists but on the way out; says what replaces it. |
| `Superseded` | Replaced; links to what replaces it. |

## 5. When to write or update

Documentation is required for behavior-changing work, written as the final step once the code is built, tested, and QA-passed.

- A new feature gets a new document from `TEMPLATE.md`.
- A change to an existing feature that alters behavior, a contract (API, schema, config), security or RBAC, or operations updates that feature's document, with `version` and `updated` bumped.
- An architectural decision gets a new ADR.

No document is needed for a pure refactor with no behavior change, formatting, a typo, a rename that changes nothing observable, or a dependency bump that changes nothing visible. When in doubt, a one-line note in the revision history is cheaper than a stale document.

## 6. Writing a good document

Lead with the summary so a reader understands the feature in the first ten seconds. Stay specific to this codebase: cite real files as `path:line`, real env vars, real routes, not generic advice. Capture the trade-offs and the alternatives that were rejected, because that is what saves the next engineer. Add a Mermaid diagram for a non-trivial cross-service flow; text alone does not convey it. Keep the core sections always present and omit an optional section rather than filling it with "N/A".

The sections that make an Orcastra document worth reading:

- RBAC and role behavior: how Admin, Partner, and Tenant each experience the feature, with the gating and redirects.
- Configuration: the env vars, Vault material, and flags that control the feature, each with its default and effect.
- Security and privacy: authorization boundaries, IDOR and tenant or cluster isolation, secret and certificate handling, PII.
- Operations: migrations, rollout, rollback, monitoring, and any cluster or Vault steps.
- Testing evidence: what is covered, how to run it, and the QA actually performed. This is the proof the feature works.

## 7. House writing style

Documentation must read as clear, senior-engineer prose. Precise and plain beats polished and vague.

- Be specific and verifiable. Ground every claim in the code and cite real `path:line`. Do not generalize or pad.
- Use real subheadings, tables, and prose for structure. Do not use a bold label followed by a colon as a substitute for structure.
- Use bold rarely, for a single key term, never as a list crutch. No emoji in body text or headings.
- Cut filler and hedging ("it is worth noting", "importantly", "in essence") and marketing words ("powerful", "seamless", "robust", "leverage", "facilitate"). Write "use", not "utilize".
- Vary sentence and section length. Avoid mechanically uniform, identically shaped sections.
- Hyphens with spaces ( - ), never em-dashes. Straight quotes. Reach for "for example" or "such as" rather than peppering the text with "e.g.".

## 8. Maintenance

Update the document in the same change that alters the feature; a document that lags the code is a liability. Bump `version` and `updated` on every substantive edit and append a row to the revision history. When a feature is removed, set the status to `Deprecated` or `Superseded` and say what replaced it, rather than silently deleting the document.

## 9. Index

Features, grouped by domain. Keep this current as documents are added.

<!-- INDEX:START -->
| Domain | Document | Status |
|--------|----------|--------|
| ui | [table-sorting](ui/table-sorting.md) | Active |
<!-- INDEX:END -->

For recorded architectural decisions, see [adr/README.md](adr/README.md).
