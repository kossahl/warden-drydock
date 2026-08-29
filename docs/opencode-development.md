# OpenCode development setup

Warden Drydock includes project-local OpenCode configuration under `.opencode/`.
The files use OpenCode V1-compatible syntax so the desktop application can load
them. OpenCode V2 also reads supported V1 configuration and normalizes it in
memory. GitHub Project 1 remains the plan and status source. The OpenCode files
only define how a task reads and executes that plan.

## Start work

Start OpenCode in the repository and run:

```text
/continue-mvp
```

The command activates `drydock-orchestrator`. It reads `AGENTS.md`, the private
Project README and fields, the next eligible versioned Work Package, current
remote `master`, and live references. It must stop instead of guessing when a
decision, dependency, permission, or baseline is missing.

OpenCode needs working GitHub CLI authentication with repository and Projects
read access. GitHub writes and Git publication remain approval-gated.

## Roles

The orchestrator can delegate to the same ten roles used by the Project:

- Product Strategist
- Product Designer
- Architect
- Core Implementer
- Adapter Specialist
- Hosted Backend Implementer
- Web Frontend Implementer
- Test Engineer
- Reviewer
- Docs Maintainer

Agent files do not select a provider or model. Subagents inherit the active
session model. OpenCode discovers the canonical skills directly from
`.agents/skills`; OpenCode-specific copies are not maintained.

Architect, Product Strategist, and Reviewer cannot edit files. Other role
agents may edit only their assigned Work Package ownership. Direct GitHub and
Git mutation commands are denied to subagents. Known read and verification
commands are allowlisted; other shell commands require approval. The
orchestrator owns Git and GitHub operations and invokes the Reviewer after
implementation until no actionable findings remain. These controls reduce
accidental authority use; they are not a sandbox against deliberately wrapped
commands, so agent instructions and approval review remain part of the boundary.

## Boundaries

- No OpenCode agent replaces deterministic Drydock operations.
- No agent gains independent GitHub identity or authority.
- No campaign content, credential, or private log belongs in GitHub.
- No Agent Ascendry, self-evolution, automatic polling, or phase-specific
  orchestrator is included.
- `.codex/agents` remains available for Codex. `.opencode/agents` is the
  OpenCode equivalent.

## Validation limitation

The configuration follows the current official OpenCode V1 Markdown agent and
permission formats. The `opencode` executable was not available in the shell
used to prepare this configuration, so a fresh OpenCode session must perform
the final discovery check:

1. confirm `drydock-orchestrator` is the default agent;
2. confirm all ten role agents appear;
3. run `/continue-mvp`;
4. confirm it selects a current `Ready` item and returns a valid receipt state;
5. stop before implementation if this is only a discovery test.

Official references:

- https://opencode.ai/docs/agents
- https://opencode.ai/docs/permissions
- https://opencode.ai/docs/commands
- https://opencode.ai/docs/instructions
- https://opencode.ai/docs/skills
- https://opencode.ai/v2/docs/migrate-v1
