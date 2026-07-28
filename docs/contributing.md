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
the release-process guidance in `docs/release.md` rather than opening a public
issue.