# Grounded AI and live backend

The personal-pilot backend provides provider-neutral grounded generation and
live-session application services. Deterministic retrieval creates and persists
a pinned source envelope and digest before provider dispatch. The OpenAI adapter
uses only `gpt-5.6-luna`, reads `OPENAI_API_KEY` from the process environment,
sets `store: false`, exposes no hosted tools, and normalizes provider output into
ordered Draft events.

Provider configuration and explicit current data-transfer consent gate
generation. Provider failures persist a terminal failure without changing
campaign snapshots. Stream disconnect is not cancellation and never authorizes
mutation; clients can resume from the last observed sequence.

Live sessions retain their starting `base_revision` even when the reported head
advances. Confirmed table facts may augment grounding; unresolved questions are
stored separately and are never grounding evidence. Controller epochs,
workflow versions, and device operation receipts reject stale or conflicting
mutations. Provider availability is independent of typed Capture.

Recovery can disable new provider and live entrypoints while retaining persisted
source envelopes, Drafts, sessions, and captures for inspection and later
resumption. HTTP routing and device-storage technology remain owned by their
downstream work packages.
