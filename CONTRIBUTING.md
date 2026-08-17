# Contributing to Orcastra Dashboard

Thanks for your interest in improving Orcastra Dashboard.

## Contribution Model

This project uses an issue-first workflow.

1. Open an issue first and explain the problem, expected result, and context.
2. Maintainers will triage and decide implementation scope.
3. Maintainers implement core feature and architecture changes.

External pull requests are currently accepted for:

- Documentation fixes and clarity improvements
- Typo fixes
- Small non-breaking test improvements

For code changes, always link a previously approved issue.

## Before Opening an Issue

- Search existing issues to avoid duplicates.
- For broad discussion or early ideation, start at https://discourse.orca.id first.
- Include reproduction steps and expected behavior.
- Add environment details: OS, Docker version, browser version, and deployment mode.

## Branch and Commit Guidance

- Branch naming: `docs/<short-topic>`, `fix/<short-topic>`, `chore/<short-topic>`
- Keep commits focused and small.
- Use clear commit messages that explain impact.

## Pull Request Checklist

- PR references an issue number.
- Scope stays aligned with approved issue.
- No secrets, tokens, private keys, or real credentials.
- Docs are updated when behavior or configuration changes.
- Backward compatibility is explained if relevant.

## Local Validation

Use these commands before proposing changes:

```bash
make config
make build
make up
make health
```

First time in a fresh clone, point git at the repo's hooks so commit messages are validated
locally the same way CI validates them:

```bash
make setup-hooks
cd frontend && npm install   # the commit-msg hook runs commitlint from here
```

If your change touches backend or frontend behavior, also run:

```bash
make test-backend     # pytest, needs no database or cluster
make lint-backend     # ruff, bug-shaped rules only
make test-frontend    # typecheck + structural guards
make lint-frontend    # eslint
```

These are the same gates CI runs (`.github/workflows/ci.yml`), so a green local run means a
green pipeline.

## Security Reports

Do not report vulnerabilities in public issues.

Use private disclosure workflow in [SECURITY.md](SECURITY.md).
