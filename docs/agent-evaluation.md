# Agent Evaluation

The agent benchmark is deterministic evaluation tooling, not part of Warden
Drydock's runtime. It validates manually or externally recorded results; it
does not execute agents, emulate Codex routing, or grade free text with a model.

## Report workflow

Create a JSON report using the `benchmark_report` definition in
`tests/agent_evals/schema.json`. Reference `routing-v1`, `handoff-v1`, or both
in `dataset_refs`. Every run must use a case from a referenced dataset and a
unique `(case_id, attempt)` pair.

`tests/agent_evals/baselines/example-not-run.json` is a deliberately unevaluated
synthetic example. It demonstrates unavailable telemetry and makes no quality,
routing, or execution claim.

Record the exact configuration in `environment.configuration_ref`. This can be
a Git revision, content hash, file path plus snapshot label, or another stable
reference. It must not imply that the working tree was clean. Record model and
reasoning effort when known; use `null` when they are unavailable.

Structurally validate a report:

```powershell
python tests/agent_evals/benchmark.py --validate-only path/to/report.json
```

Compare two validated reports:

```powershell
python tests/agent_evals/benchmark.py --compare path/to/before.json path/to/after.json
```

Both commands emit stable, sorted JSON. Invalid reports produce an error on
standard error and exit with status 2. Extra report fields, including an
unexplained aggregate score, are rejected.

`benchmark.py --validate-only` is structural-only. It proves schema and basic
cross-field consistency, not that a quality label has evaluator evidence. WP6
acceptance must use the strong paired-experiment validation below.

The default datasets are the repository routing and handoff fixtures. A custom
dataset may be supplied repeatedly with `--dataset PATH`; custom datasets
replace the defaults for that invocation. Dataset references can use the
dataset ID, filename, or resolved path.

## Interpretation

Routing, handoff, and task quality remain separate categorical results. The
comparison lists their before/after transitions and does not manufacture a
weighted score. Mismatched case sets and unmatched attempts are explicit; they
must be resolved before claiming a like-for-like benchmark.

Telemetry uses one of three provenance states:

- `measured`: supplied by a trustworthy direct source;
- `estimated`: calculated by a named method, such as a specified tokenizer;
- `unavailable`: `value` and `method` are both `null`.

Numeric telemetry is compared only when both reports use the same provenance
state and exact method. Unavailable values are never treated as zero. `tiktoken`
may be named as an estimation method by an external recorder, but the benchmark
does not import it and the repository does not depend on it. Live Codex token
telemetry is unavailable unless the platform explicitly supplies it.

## Adoption rule

Do not set acceptance thresholds until repeated baseline evidence exists. Adopt
an agent configuration change only when it violates no protected invariant,
causes no quality regression, retains required handoff evidence, and shows a
token, runtime, or correction-round benefit through comparable telemetry.
Record the interpreter, model, reasoning effort, configuration reference, and
measurement method used for each run.

## Controlled medium/high experiment

Create pinned pending templates from existing case IDs. Supply the timestamp
explicitly so regeneration is deterministic:

```powershell
python tests/agent_evals/experiment.py create `
  --output-dir .agent-evals/experiments/effort-core `
  --experiment-id effort-core `
  --case routing-core-001 `
  --case handoff-core-complete-implementation `
  --attempts 3 `
  --model MODEL-ID `
  --medium-config .codex/agents/core_implementer.toml@MEDIUM-HASH `
  --high-config .codex/agents/core_implementer.toml@HIGH-HASH `
  --rubric-version handoff-rubric-v1 `
  --created-at 2026-08-10T15:00:00+02:00
```

The command creates `manifest.json`, one schema-v1 report per effort, and one
empty observation file per effort. The manifest fixes the exact dataset, case,
attempt, model, effort, configuration, rubric, timestamp, and fresh-context
protocol. Medium and high always receive identical `(case_id, attempt)` sets.
The command does not execute an agent.

For every run, an evaluator completes one observation with:

- requested and observed agent/sequence;
- separate routing, handoff, and task judgments;
- evaluator type and stable identifier;
- pinned rubric, model, effort, configuration, and evaluation timestamp;
- repository-relative or SHA-256 evidence and verification references;
- verification status and telemetry with measured, estimated, or unavailable
  provenance.

Copy those judgments and telemetry into the corresponding schema-v1 report.
Keep report `notes` null. Then run the strong validation:

```powershell
python tests/agent_evals/experiment.py validate `
  --manifest .agent-evals/experiments/effort-core/manifest.json `
  --medium-report .agent-evals/experiments/effort-core/report-medium.json `
  --medium-evidence .agent-evals/experiments/effort-core/evidence-medium.json `
  --high-report .agent-evals/experiments/effort-core/report-high.json `
  --high-evidence .agent-evals/experiments/effort-core/evidence-high.json
```

Only after this returns `valid_complete_pair` should the two reports be passed
to `benchmark.py --compare`.

Strong validation rejects incomplete observations; mismatched cases, attempts,
efforts, models, configurations, datasets, rubrics, quality labels, and
telemetry; contradictory outcomes; and unsafe evidence references. It validates
provenance and consistency, not the evaluator's semantic judgment.

Committed reports and evidence manifests must not contain raw agent output,
transcripts, hidden reasoning, secrets, personal data, campaign canon, absolute
personal paths, or untrusted free-form notes. If raw local evidence is necessary
for review, retain it under the ignored, retention-bound
`.agent-evals/experiments/` runtime path and reference only a sanitized
repository-relative identifier or SHA-256 digest. Raw artifacts are never
required in Git.

Model output is nondeterministic even with pinned inputs. Use repeated attempts
(three is a useful starting point, not an acceptance threshold), a fresh context
for every case/attempt/effort, and randomized or concealed effort labels during
review where practical. Use blind dual review for borderline or disputed cases;
preserve disagreements instead of averaging them into a score.

Run the focused tests directly:

```powershell
python tests/agent_evals/test_benchmark.py
python tests/agent_evals/test_experiment.py
```
