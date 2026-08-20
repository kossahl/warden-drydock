# Grounded AI and live backend

The personal-pilot backend provides provider-neutral grounded generation and
live-session application services. Deterministic retrieval creates and persists
a pinned source envelope and digest before provider dispatch. The OpenAI adapter
uses only `gpt-5.6-luna`. Local development may supply `OPENAI_API_KEY` directly;
the Compose pilot reads the credential from the app-only `provider_secrets`
volume through `OPENAI_API_KEY_FILE`. The adapter sets `store: false`, exposes
no hosted tools, and normalizes provider output into
ordered Draft events. Every Responses API request has a provider-native hard
output limit: normal personal-pilot adapters default to 2,048 output tokens,
while the separately authorized live smoke uses an explicitly configured
512-token adapter instance. Limits must be finite positive integers within the
model's supported output bound. A smoke instance does not change the default or
another adapter instance. Configuration alone does not authorize or perform a
live provider request; the approved smoke remains an explicit operator action.
The file path must resolve inside `DRYDOCK_SECRETS`; missing, empty, ambiguous,
or escaped credential configuration fails closed. Credential replacement
changes the consent-bound fingerprint without exposing the value.

Provider configuration and explicit current data-transfer consent gate
generation. Configuration readiness checks only for a local credential and does
not call the Models API or require Models-read permission. The first authorized,
bounded Responses request is the capability and access check; no separate
inference probe is sent. Authentication, authorization, and transport failures
persist a sanitized terminal failure without provider bodies, credentials, or
campaign content and without changing campaign snapshots. Stream disconnect is
not cancellation and never authorizes mutation; clients can resume from the
last observed sequence.

Live sessions retain their starting `base_revision` even when the reported head
advances. Confirmed table facts may augment grounding; unresolved questions are
stored separately and are never grounding evidence. Controller epochs,
workflow versions, and device operation receipts reject stale or conflicting
mutations. Provider availability is independent of typed Capture.

Recovery can disable new provider and live entrypoints while retaining persisted
source envelopes, Drafts, sessions, and captures for inspection and later
resumption. The local HTTP proposal slice now routes grounded Ask start,
event resumption, and terminal inspection against this backend. Device-storage
technology and the broader Live Cockpit remain owned by downstream work.
