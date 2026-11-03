# Contributing to Orcastra CMP

Thanks for your interest in improving Orcastra CMP.

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

Those are the quick checks while you work. Before you push, run the whole pipeline on your
machine:

```bash
make ci-local                              # every job in .github/workflows/ci.yml
make ci-local STAGES="guards frontend"     # a subset of: commits guards backend frontend e2e
make ci-local E2E_PROJECTS=desktop-light   # one browser project instead of all four
```

GitHub runs CI only for pull requests (with one browser project) and when someone starts it from
the Actions tab, where "Run workflow" can ask for all four browser projects. A push straight to a
branch is not checked there, so `make ci-local` is the gate for it.

The runner reads the guard list, the toolchain versions and the browser job's environment from
`ci.yml`, so it cannot fall behind the pipeline. The pinned Node and Playwright are installed once
under `~/.cache/orcastra-ci-local`, and the journal PostgreSQL contracts get a throwaway database
container when Docker is available (`CI_LOCAL_POSTGRES=skip` to opt out). It tests the working
tree rather than a commit, so commit first if you want the result CI would see. Commit messages are
linted against the branch's upstream; set `CI_LOCAL_BASE=<remote>/<branch>` if it has none.

## Versioning

The project follows [Semantic Versioning](https://semver.org/):

- MAJOR (x.0.0): breaking changes incompatible with previous versions
- MINOR (0.x.0): new features with backward compatibility
- PATCH (0.0.x): bug fixes without new features
- RC (x.x.x-RCn): release candidate for testing before a stable release

The version is set by `APP_VERSION` in `.env` and by `version` in `frontend/package.json`.
`make version-sync` propagates it to the other files, but that target and its script
(`scripts/sync_version.py`) live on the operator VM, not in the repository, so in a fresh clone set
both by hand. The release badge in `README.md` is
updated by hand when a release is published.

## Security Reports

Do not report vulnerabilities in public issues.

Use private disclosure workflow in [SECURITY.md](SECURITY.md).
