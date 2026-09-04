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

## Skills and roles

The installed skills are reusable procedures. The Drydock agents remain the
authority for role selection, file ownership, permissions, Work Package scope,
and repository invariants.

| Role | Matt skill support |
| --- | --- |
| Product Strategist | `to-spec`, `to-tickets` |
| Product Designer | `prototype` |
| Architect | `codebase-design`, `improve-codebase-architecture` |
| Core or Hosted Implementer | `tdd`, `implement` |
| Adapter Specialist | `tdd` where applicable |
| Web Frontend Implementer | `prototype`, `tdd`, `implement` where applicable |
| Test Engineer | `tdd` |
| Reviewer | `verify-drydock-change`, `code-review` |
| Docs Maintainer | `writing-for-agents` where applicable |

The repository-local `plan-drydock-change` and `verify-drydock-change` skills
remain authoritative for Drydock-specific planning and verification. Matt's
skills must not bypass the private GitHub Project, versioned Work Packages,
baseline checks, ownership rules, or parent-owned GitHub communication.

Some upstream skill instructions refer to Claude's `Skill` tool or
generic sub-agents. OpenCode should map those references to the project role
agents and the table above. If nested invocation is unavailable, invoke the
referenced skill directly and record that limitation in the handoff.

Agent files do not select a provider or model. Subagents inherit the active
session model. OpenCode discovers the canonical skills directly from
`.agents/skills`; OpenCode-specific copies are not maintained for the
repository-local skills. Matt's global skills are loaded from the user's
OpenCode skill directory.

OpenCode mirrors the role descriptions and instructions in `.codex/agents`; the
Codex definitions are authoritative. Architect, Product Strategist, and
Reviewer remain read-only roles. Other role agents do not receive additional
OpenCode command restrictions. Git and GitHub ownership remains the process
rule in `AGENTS.md`: the parent or orchestrator owns branches, commits, pull
requests, and public communication. The orchestrator invokes the Reviewer after
implementation until no actionable findings remain.

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
