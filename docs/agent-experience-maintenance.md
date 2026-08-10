# Agent Experience Queue Maintenance

`.codex/hooks/agent_experience_maintenance.py` audits the ignored local experience queue without changing it. It is a deterministic health check for retrospective preparation, not an autonomous memory or learning system.

## Audit

From the repository root:

```powershell
python .codex/hooks/agent_experience_maintenance.py
```

The default queue is the repository-local `.agent-experience/pending/`, and the default optional processing state is its sibling `.agent-experience/processed.json`. Supplying `--queue-root` and `--processed-state` supports isolated evaluation and tests. The command emits stable, filename-sorted JSON containing counts and filenames only; it never includes event values or transcript contents.

Optional retention measurements report candidates but never enforce policy:

```powershell
python .codex/hooks/agent_experience_maintenance.py --older-than-days 30 --larger-than-bytes 12000
```

The project has no default age or size threshold. Choosing either remains a user governance decision after observing a representative queue baseline.

## Event classifications

- `valid_v1` and `valid_v2`: supported, structurally valid envelopes.
- `malformed_json`: invalid UTF-8 or JSON.
- `non_object`: valid JSON whose root is not an object.
- `unknown_schema`: absent, unsupported, or structurally invalid schema.
- `identity_mismatch`: a v2 `event_id` differs from the actual filename stem.
- `oversized_v2`: a v2 event exceeds 16,384 UTF-8 bytes.
- `ignored`: non-JSON or non-regular directory entries.

One invalid event does not block classification of the rest. Valid events are also reported as `processed` or `unprocessed`. Missing transcript references are identified by queue filename after an existence check only; the tool never opens the transcript and never uses an event-provided path as a mutation target.

## Processed-state contract

The optional compact state file has this schema:

```json
{
  "schema_version": 1,
  "events": {
    "session--turn.json": {
      "status": "processed",
      "processed_at": "2026-08-10T18:00:00Z"
    }
  }
}
```

Keys are actual queue filenames, not raw session IDs or event-provided identifiers. A corrupt or structurally invalid status file is reported and ignored in full; it never causes queue mutation or blocks event auditing. This tool deliberately does not infer completion or write processing state. The retrospective must explicitly establish that evidence was handled before another reviewed mechanism records that fact.

## Safety boundaries

- No event or transcript is deleted, moved, rewritten, or archived.
- No status file is created or changed.
- No transcript is read.
- No semantic deduplication or interpretation is attempted.
- Retention options only report filenames meeting explicit thresholds.
- Tests use temporary directories and never inspect or mutate the real project queue.

Audit results are operational metadata. They must not be copied into durable evolution guidance as proof of a lesson without the evidence and review required by `docs/agent-evolution.md`.
