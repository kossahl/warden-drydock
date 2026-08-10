# Agent Evaluation Contract

This directory contains versioned, deterministic contracts for evaluating the
repository's development agents. It is test infrastructure only and must not
change Warden Drydock product, adapter, generated-campaign, or canon behavior.

## Files

- `schema.json` defines routing datasets, handoff datasets, and benchmark
  reports using JSON Schema Draft 2020-12.
- `__init__.py` is an empty package marker.
- Later work packages own fixtures, rubrics, validators, and benchmark tooling.

`schema_version` is mandatory and currently fixed at `1`. Schema validation
checks document shape. Deterministic fixture validators own the cross-field
invariants below, while a rubric or reviewer owns semantic judgments.

## Fixture conventions

- Store UTF-8 JSON with two-space indentation and a final newline.
- Use stable lowercase IDs matching `^[a-z0-9][a-z0-9._-]*$`; do not embed
  random values or dates in case IDs.
- Sort cases by `case_id`. Sort agent and tag arrays unless `sequence` records
  intentional execution order.
- Use only synthetic prompts and handoffs or sanitized repository examples.
- Never store secrets, personal data, raw user or assistant transcripts,
  hidden reasoning, campaign canon, absolute personal paths, or untrusted web
  content.
- State a rationale for every expected result. Do not encode unexplained labels.
- Allow multiple valid agents only when they are genuinely equivalent. Record a
  required multi-agent order in `sequence`, not only in prose.
- Represent unavailable telemetry as `status: "unavailable"`, `value: null`,
  and `method: null`. Measured and estimated values require a non-negative value
  and a non-empty method. Never infer or invent missing telemetry.

## Routing invariants

- `valid_agents` and `forbidden_agents` are disjoint.
- Required delegation has at least one valid agent.
- Forbidden delegation has no valid agents and no sequence.
- Every agent in `sequence` also occurs in `valid_agents`.
- Case IDs are unique within a dataset.
- Agent names match the eight files under `.codex/agents/`.

Example:

```json
{
  "schema_version": 1,
  "kind": "routing_cases",
  "dataset_id": "routing-v1",
  "cases": [
    {
      "case_id": "routing-adapter-001",
      "prompt": "Add a Mothership NPC template field and its adapter validation.",
      "expected": {
        "delegation": "required",
        "valid_agents": ["adapter_specialist"],
        "forbidden_agents": ["core_implementer"],
        "sequence": null,
        "rationale": "Mothership-specific templates and validation belong to the adapter."
      },
      "metadata": {
        "source": "synthetic",
        "tags": ["adapter", "positive"]
      }
    }
  ]
}
```

## Handoff invariants

The schema records expected concepts but does not claim to understand whether
free text contains them. Later deterministic checks may validate structure and
explicit annotations; the semantic rubric evaluates meaning. It must not
require fixed headings, fixed ordering, empty sections, or verbosity. A compact
handoff passes when it preserves every decision-relevant fact required by its
scenario.

Example:

```json
{
  "schema_version": 1,
  "kind": "handoff_cases",
  "dataset_id": "handoff-v1",
  "cases": [
    {
      "case_id": "handoff-core-001",
      "agent": "core_implementer",
      "scenario": "Implementation succeeded and focused tests passed.",
      "handoff": "Changed tests/agent_evals/schema.json. Added the versioned contract. `python -m unittest tests.agent_evals.test_schema` passed. Residual risk: Draft 2020-12 support depends on the selected validator.",
      "expected": {
        "verdict": "complete",
        "required_concepts": ["changed_files", "outcome", "risk", "verification"],
        "forbidden_content": ["campaign_canon", "invented_telemetry", "raw_transcript", "secret", "unsupported_success_claim"],
        "rationale": "It preserves the delivered behavior, file, direct verification, and residual risk."
      },
      "metadata": {
        "source": "synthetic",
        "tags": ["complete", "implementation"]
      }
    }
  ]
}
```

## Benchmark invariants

- Each `(case_id, attempt)` pair is unique.
- Every case ID exists in one of `dataset_refs`.
- A `not_run` result uses `not_evaluated` for every quality dimension.
- Reports preserve routing, handoff, and task results separately. They do not
  manufacture a weighted aggregate score.
- Before/after comparisons use identical case-ID sets or explicitly report the
  mismatch.
- `configuration_ref` may be a path, content hash, or Git revision; reports
  must state the exact reference used and must not assume a clean worktree.

## Non-goals

This contract does not expose or emulate a Codex routing API, run agents, grade
responses with a model, prescribe token targets, archive transcripts, add
semantic memory, modify agent prompts, change reasoning effort, or permit
automatic self-modification. It introduces no PyYAML or runtime JSON Schema
dependency. A later validator may use an already available Draft 2020-12
implementation, but basic repository tests must remain portable.

Split the schemas only if their versions need to evolve independently. Add
grader provenance or telemetry fields only after a real, trustworthy source
exists.
