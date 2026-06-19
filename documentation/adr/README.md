# Architecture Decision Records (ADRs)

An ADR captures one significant architectural or cross-cutting decision: the context that forced it, the decision taken, and the consequences. ADRs are how the team remembers why the system is the way it is.

## When to write one

Write an ADR when a decision is hard to reverse or touches many parts of the system, such as:

- choosing a pattern, library, or protocol with long-term lock-in,
- a cross-cutting approach to security, RBAC, or the data model,
- how to talk to LXD/Incus clusters or manage certificates and secrets,
- a build-versus-buy choice, or a rollout strategy.

Routine feature work does not need an ADR; it needs a feature document. An ADR is for the decision, a feature document is for the feature.

## Rules

Files are `NNNN-short-title.md`, zero-padded and sequential (`0001`, `0002`, and so on). Once a record is `Accepted`, do not rewrite it. If the decision changes, write a new ADR and set the old one to `Superseded` with a link. Status moves `Proposed` to `Accepted`, then optionally to `Superseded` or `Deprecated`. Each record states enough context to make sense years later on its own.

## Index

<!-- ADR-INDEX:START -->
| # | Title | Status |
|---|-------|--------|
| [0001](0001-adopt-feature-documentation-standard.md) | Adopt a versioned feature-documentation standard | Accepted |
<!-- ADR-INDEX:END -->

Template: [TEMPLATE.md](TEMPLATE.md).
