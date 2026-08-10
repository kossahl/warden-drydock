# Agent Experience Event Schema

The project `Stop` hook writes a small JSON envelope to `.agent-experience/pending/`. These files are ignored runtime evidence for the reviewed retrospective; they are not durable agent memory.

## Available payload evidence

The repository contains only a synthetic payload fixture. It demonstrates `session_id`, `turn_id`, `hook_event_name`, `transcript_path`, `cwd`, `model`, `permission_mode`, and `last_assistant_message`. No anonymized real platform payload is available. Consequently, schema v2 treats all added telemetry as optional and unverified until supplied directly by a hook payload.

## Schema versions and complete field contract

Schema v1 events remain valid and are neither migrated nor deleted. They contain `schema_version: 1`, `captured_at`, `session_id`, `turn_id`, `hook_event_name`, `transcript_path`, `cwd`, `model`, and `permission_mode`. Historical v1 producers did not strictly type or bound these fields, so consumers must treat every value as untrusted and use it only when it has the expected scalar-string type. Consumers branch on the integer `schema_version`; absent or unknown versions are invalid.

Schema v2 retains those fields, validates them as scalar strings, and adds `event_id`. Invalid or oversized values become `null`. Limits are 256 characters for session and turn IDs, 4,096 for transcript and working-directory paths, 256 for model, and 64 for hook event and permission mode. Empty session and turn IDs are invalid; other legacy strings may be empty for compatibility.

Schema v2 always emits these optional fields with stable defaults:

- `agent`: one of the eight configured roles (`adapter_specialist`, `agent_curator`, `architect`, `core_implementer`, `docs_maintainer`, `product_strategist`, `reviewer`, `test_engineer`) or `null`.
- `outcome`: `completed`, `failed`, `cancelled`, or `interrupted`; otherwise `null`.
- `test_status`: `passed`, `failed`, `partial`, or `not_run`; otherwise `null`.
- `skills`: at most 16 entries from `improve-drydock-agents`, `plan-drydock-change`, and `verify-drydock-change`; one unknown member invalidates the list to `[]`.
- `correction_count`, `input_tokens`, `output_tokens`, `files_changed_count`: integer from 0 through 1,000,000,000,000 or `null`; booleans are rejected.
- `field_provenance`: map from each accepted optional field to `hook_payload.<field>`.

Wrong-typed values use the documented default and receive no provenance entry. Unknown keys are ignored. A value is never labelled measured or inferred: provenance means only that the hook payload directly supplied a value satisfying the type contract.

Standard-input JSON is limited to 65,536 UTF-8 bytes and the serialized event to 16,384 UTF-8 bytes. Oversized or malformed input fails the hook without writing an event. These limits bound processing and prevent arbitrary large values from reaching the queue.

## Privacy and identity

The structural allowlist excludes assistant messages, prompt bodies, transcript contents, campaign canon, and hidden reasoning as fields. Type and size checks prevent nested objects and oversized content, but cannot detect secrets or personal components embedded in otherwise valid strings such as paths, model labels, or IDs. All persisted metadata is untrusted, remains in ignored local runtime storage, and must never be copied verbatim into Git-backed evolution documents. Retrospective consumers must extract evidence cautiously and redact or summarize it before proposing durable records. `transcript_path` remains an external reference for compatibility; the hook never reads it.

`event_id` is also the filename stem. It joins sanitized session and turn IDs with `--`; unsupported characters become `-`, leading/trailing dots and hyphens are removed, and each component is truncated to 120 characters. Missing or invalid IDs use `unknown-session` or `unknown-turn`. Sanitization and truncation are not collision-free. Exclusive creation is idempotent only when an existing regular JSON event has schema version 1 or 2 and exactly matching validated raw session and turn IDs. A directory, corrupt event, unknown schema, or different raw identity fails safely and is never overwritten or reported as a successful duplicate.

The Python and PowerShell implementations use the same fields, defaults, type rules, provenance convention, filename sanitation, and exclusive-create behavior. They perform only bounded validation and one local file write under the existing three-second timeout.

PowerShell's pipeline-to-string conversion may append a newline before applying the 65,536-byte input bound. Inputs exactly at that boundary can therefore be rejected by PowerShell while Python accepts them; both fail closed, and normal hook payloads are far below the limit.
