# Contributing to Warden Drydock

Thank you for your interest in improving Warden Drydock. Every change to this
repository — features, bug fixes, documentation, or refactors — is developed
on a focused branch and delivered through a pull request.

## Policy: PR-first contribution workflow

1. **One focused branch per change.** Create a branch whose name states the
   scope of the change (see [Branch naming](#branch-naming) below).
2. **Focused commits.** Each commit should contain one coherent task. Avoid
   bundling unrelated formatting fixes with a behavior change.
3. **Required checks pass locally before opening a PR.**
   - `python -m unittest discover -s tests -v`
   - `python -m warden_drydock --help`
   - `git diff --check`
   - `./scripts/review-check.sh` (the canonical local CI gate)
4. **PR review is required.** At least one maintainer approval is required
   before merge. Maintainers may request changes; respond with follow-up
   commits on the same branch rather than new PRs.
5. **Merge expectations.** Squash or rebase-merge into `master`. The merge
   commit (or squashed commit) must reference the issue number it closes.

## Branch naming

Use a short kebab-cased prefix that describes the change family, followed by
a concise description:

| Prefix       | Use for                                                |
|--------------|--------------------------------------------------------|
| `feat/`      | New user-facing functionality                          |
| `fix/`       | Bug fixes                                              |
| `docs/`      | Documentation-only changes                             |
| `refactor/`  | Internal changes that do not alter behavior            |
| `test/`      | Test-only changes                                      |
| `chore/`     | Tooling, CI, dependency, or housekeeping changes      |

Examples:

- `feat/declarative-secrecy-validation`
- `fix/upgrade-conflict-resolution`
- `docs/pr-first-workflow`

## Parallel changes

When you need to make two unrelated changes at once, **do not stack them on a
single branch**. Either:

- open two branches from `master` and submit two PRs, or
- use a git worktree per change so the working trees stay independent.

Mixing unrelated changes in one PR slows review, complicates bisect, and makes
it harder to revert one change without affecting the other.

## Working with AI coding agents

The framework is operated primarily by AI coding agents. The same PR-first
rules apply to them:

- An agent must work on a dedicated branch, not directly on `master`.
- An agent must commit focused units of change and stop for review.
- An agent must not silently amend or force-push commits that humans have
  reviewed.
- See `AGENTS.md` for the durable instruction set given to agents.

The durable, repo-wide instruction for agents lives in `AGENTS.md` at the
repository root. If you change contribution policy in a way that affects how
agents operate, update `AGENTS.md` in the same PR.

## Local environment

Warden Drydock requires Python 3.11 or newer and has no runtime dependencies.

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m warden_drydock --help
```

Before opening a PR, run `./scripts/review-check.sh`. It uses digest-pinned
Python 3.11/3.13 and Node/npm containers to mirror CI, including Chromium
browser tests, the clean onboarding smoke test, live PostgreSQL checks, and
whitespace validation. The mounted checkout may receive the ignored
`web/node_modules/`, `web/test-results/`, and `web/dist/` build artifacts;
remove them when a clean host tree is needed. Set `DRYDOCK_CI_BASE_REF` if
`origin/master` is not available locally.

## Continuous integration scope

Every pull request runs one canonical Python 3.11 lane. That lane checks the
committed change range for whitespace errors, runs the full unit suite and CLI
help, builds the package, and installs the built wheel in a clean environment
for the standalone onboarding smoke test. Pushes to `master` and version tags
(`v*`) run the same canonical lane plus a Python 3.13 compatibility lane that
exercises the unit and package-build boundaries. Feature-branch pushes do not
start a second run alongside the pull-request run.

CI establishes executable behavior and machine-checkable structure, schema,
and references. Governance tests preserve those deterministic contracts, such
as required fields and valid cross-references; they do not judge whether an ADR,
product or UX decision is substantively correct, and they do not prove that an
agent handoff is semantically complete. Maintainer approval and the applicable
human or independent role review remain required for those judgments.

## Pull request checklist

Before you mark a PR ready for review:

- [ ] Branch is up to date with `master`
- [ ] Tests pass locally
- [ ] `git diff --check` reports no whitespace errors
- [ ] Commit messages explain *why*, not just *what*
- [ ] Documentation under `docs/` is updated if behavior changed
- [ ] `CHANGELOG.md` is updated for user-visible changes
- [ ] PR description references the issue it closes

## Reporting issues

Open an issue describing the observed behavior, the expected behavior, and
the smallest reproduction you can provide. For security disclosures, follow
[`SECURITY.md`](../SECURITY.md) and never open a public issue.
