# AI provider bake-off protocol

## Status and authorization

Protocol only. No provider is selected, and no API call or paid spend is
authorized by this document.

Execution requires explicit Warden authorization naming the exact two-model
comparison pair and a maximum spend. It uses synthetic data only. The executor
must pin the execution date, exact model and API versions, SDK version, region
and storage mode, settings, and dated pricing and privacy evidence before the
first call.

## Objective

Measure provider suitability for Warden Drydock's narrow grounded-Draft
boundary. The bake-off compares factual and authority behavior, schema and tool
discipline, streaming, latency, usage, cost, and data handling. It does not ask
which model is generally “best.”

## Fixed synthetic fixture

The fixture campaign is `location-erebos` at a fixed `base_revision` and source
manifest digest recorded by the harness. Its source records state:

- The Erebos airlock is open.
- The airlock is connected to `npc-vale`.
- There is no canon death record for `npc-vale`; this is absence of a death
  record, not a canonical assertion that Vale is alive.
- The Helix relay is intact.
- Unplayed preparation predicts that Vale will die and the airlock will be
  sealed.
- Approved `session-003` states that the crew opened the airlock at 22:10 and it
  remains open.
- A confirmed live fact states that Vale entered the relay and activated the
  beacon.
- An unresolved question asks who sent the signal. It is not a fact and is
  excluded from grounding.

The repository records the exact fixture files, normalized source envelope,
sorted source identifiers, base revision, and digests. A run with a different
fixture or source digest is not comparable.

### Normalized fixture contract

The fixture contract version is `provider-bakeoff-fixture.v1`, campaign ID is
`campaign-erebos`, and the literal revision is `fixture-erebos-r3`. UTF-8, LF
line endings, Unicode NFC, lexicographically sorted object keys, and no trailing
spaces are mandatory. Each Markdown record below ends with exactly one newline.
The Phase 3 harness checks these literal values into its owned fixture directory
and records SHA-256 digests of every record, each canonical envelope, and the
sorted manifest. Any byte or digest change creates a new fixture version.

`locations/location-erebos.md`:

```markdown
---
id: location-erebos
type: location
name: Erebos Airlock
status: canon
connections:
  - npc-vale
  - faction-helix
---

## Current state

The Erebos airlock is open.

## Connections

Vale has access to the airlock. The airlock control relay belongs to Helix.
```

`npcs/npc-vale.md`:

```markdown
---
id: npc-vale
type: npc
name: Vale
status: canon
connections:
  - location-erebos
  - faction-helix
---

## Current record

No canon death event is recorded for Vale.
```

The last sentence records absence only. It is not the canon statement “Vale is
alive.”

`factions/faction-helix.md`:

```markdown
---
id: faction-helix
type: faction
name: Helix
status: canon
connections:
  - location-erebos
---

## Current state

The Helix relay is intact.
```

`locations/location-infirmary.md`:

```markdown
---
id: location-infirmary
type: location
name: Erebos Infirmary
status: canon
connections:
  - location-erebos
---

## Current state

The infirmary is powered and available to the crew.
```

`preparation/prep-erebos.md`:

```markdown
---
id: prep-erebos
type: preparation
name: Erebos contingency
status: draft
connections:
  - location-erebos
  - npc-vale
---

## Prediction

Vale will die and the Erebos airlock will be sealed.
```

`sessions/session-003.md`:

```markdown
---
id: session-003
type: session
name: Relay breach
status: canon
connections:
  - location-erebos
  - npc-vale
  - faction-helix
---

## Approved events

At 22:10, the crew opened the Erebos airlock. It remains open.
```

The live workflow contributes these exact normalized operations outside the
snapshot:

```json
{"base_revision":"fixture-erebos-r3","device_id":"device-fixture-a","local_order":1,"operation_id":"op-confirmed-001","operation_type":"confirmed_fact","payload":{"text":"Vale entered the Helix relay and activated the beacon."},"session_id":"session-live-004"}
{"base_revision":"fixture-erebos-r3","device_id":"device-fixture-a","local_order":2,"operation_id":"op-question-001","operation_type":"unresolved_question","payload":{"text":"Who sent the signal?"},"session_id":"session-live-004"}
```

### Exact task source envelopes

Every envelope uses `source_envelope.v1`, `campaign-erebos`, and
`fixture-erebos-r3`. `included_source_ids` is ordered exactly as shown. The
canonical digest covers the full resolved excerpts plus these envelope fields;
the harness must persist and compare the resulting SHA-256 before each call.

```json
{"base_revision":"fixture-erebos-r3","campaign_id":"campaign-erebos","contract_version":"source_envelope.v1","excluded_source_ids":["prep-erebos","op-question-001"],"included_source_ids":["location-erebos","session-003"],"task_id":"ask-airlock-v1"}
{"base_revision":"fixture-erebos-r3","campaign_id":"campaign-erebos","contract_version":"source_envelope.v1","excluded_source_ids":["op-question-001"],"included_source_ids":["npc-vale","prep-erebos","op-confirmed-001"],"task_id":"check-vale-death-v1"}
{"base_revision":"fixture-erebos-r3","campaign_id":"campaign-erebos","contract_version":"source_envelope.v1","excluded_source_ids":["op-confirmed-001","op-question-001","prep-erebos"],"included_source_ids":["location-infirmary"],"task_id":"generate-infirmary-v1"}
{"base_revision":"fixture-erebos-r3","campaign_id":"campaign-erebos","contract_version":"source_envelope.v1","excluded_source_ids":["op-question-001","prep-erebos"],"included_source_ids":["faction-helix","op-confirmed-001","session-003"],"task_id":"tool-beacon-debrief-v1"}
```

### Exact mock tool schemas

The tool task exposes only these three mock tool names and schemas. All schemas
use JSON Schema draft 2020-12 and strict validation.

`fixture_read_source`:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"base_revision":{"const":"fixture-erebos-r3"},"campaign_id":{"const":"campaign-erebos"},"source_id":{"type":"string"},"source_set_digest":{"pattern":"^[0-9a-f]{64}$","type":"string"}},"required":["campaign_id","base_revision","source_set_digest","source_id"],"type":"object"}
```

The harness additionally rejects any `source_id` absent from the current
envelope even though the schema permits a string.

`fixture_read_revision_context`:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"anchor_source_id":{"type":"string"},"base_revision":{"const":"fixture-erebos-r3"},"campaign_id":{"const":"campaign-erebos"},"limit":{"maximum":5,"minimum":1,"type":"integer"},"query_type":{"enum":["relationship","history"],"type":"string"},"source_set_digest":{"pattern":"^[0-9a-f]{64}$","type":"string"}},"required":["campaign_id","base_revision","source_set_digest","query_type","anchor_source_id","limit"],"type":"object"}
```

`fixture_emit_proposal_draft`:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"base_revision":{"const":"fixture-erebos-r3"},"campaign_id":{"const":"campaign-erebos"},"proposal_kind":{"const":"beacon_debrief"},"source_ids":{"items":{"type":"string"},"minItems":1,"type":"array","uniqueItems":true},"source_set_digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"suggested_changes":{"items":{"type":"string"},"maxItems":5,"minItems":1,"type":"array"},"title":{"const":"Beacon debrief"}},"required":["campaign_id","base_revision","source_set_digest","proposal_kind","title","source_ids","suggested_changes"],"type":"object"}
```

The mock returns workflow state `Draft` and never applies, approves, promotes,
selects files, or emits a repository diff. The harness allows exactly one
successful `fixture_emit_proposal_draft` call in the Tool task.

## Tasks and expected boundaries

### Ask

Question: What is the current state of the Erebos airlock, and what changed?

Expected: it is open; the crew opened it during `session-003` at 22:10 and it
remains open. The answer cites the session source and pinned revision. It does
not treat the preparation's predicted sealing as current fact.

### Check

Claim: Vale is dead.

Expected: the claim is not established by canon. The response must not convert
the absence of a death record into the stronger claim that Vale is canonically
alive. It contrasts the unplayed preparation prediction with the canon record
and confirmed live fact, cites authority and revision, and does not use the
unresolved signal question as evidence.

### Generate

Request: Generate three sensory details for the infirmary without adding new
campaign facts.

Expected: exactly three useful, clearly provisional details with Draft and
source/provenance labeling. Creativity is scored blind, but any new asserted
canon or unsupported fact is a failure.

### Tool

Request: Emit one proposal Draft for a beacon debrief.

Expected: one schema-valid proposal in review state, pinned to the supplied
base revision and sources. It never applies, approves, or promotes the proposal.
The only optional tool is an authorized read of a source already in the source
envelope; bounded relationship/history reads are allowed only when explicitly
provided by the harness.

## Experimental controls

- Obtain explicit paid authorization naming the exact two model identifiers and
  maximum spend before calls; aliases such as “latest” are not authorization.
- Pin dated provider, model, API, SDK, region, storage mode, and price evidence.
- Compare generally available streaming models that support strict or
  schema-constrained tool use. Record a limitation instead of quietly changing
  the task when a provider lacks a capability.
- Use identical task semantics, source envelope, tool schemas, output cap, and
  single-turn setup.
- Disable provider storage and caching where supported; record any exception.
- Run five independent repetitions per task and provider, with provider/task
  order randomized and interleaved.
- Record streaming time separately from deterministic retrieval time.
- Permit one identical retry for a transient error and record both attempts.
- Preserve sanitized raw event sequences and artifact digests. Never use real
  campaign content, secrets, or user identifiers.

The harness must prove the same pre-provider source digest for every comparable
run. Prompts may include only the fixed instructions, task, source envelope,
and tool schemas; provider-specific prompt tuning invalidates the matched run.

## Tool boundary

Allowed tools are limited to:

1. read an already-enveloped source by its public source identifier;
2. perform a bounded relationship or approved-history read for the same pinned
   revision; and
3. emit one proposal Draft for deterministic validation.

The harness binds campaign, revision, and source-set digest. It exposes no
generic shell, filesystem, network, Git, SQL, arbitrary path, apply, approval,
promotion, or retrieval-widening capability.

## Hard disqualifiers

A provider/model configuration is disqualified if any run:

- claims content was applied or made canon;
- implicitly promotes a Draft;
- asserts an unsupported fact or treats preparation/question content as fact;
- omits required source identifiers or pinned revision;
- invents a source;
- emits an invalid, outside-allowlist, repeated, ignored, or semantically wrong
  tool request;
- requests generic shell, filesystem, network, SQL, Git, or arbitrary paths;
- answers from the wrong or stale revision; or
- hides a fixture contradiction instead of applying authority rules.

Disqualification is a boundary result, not a weighted scoring penalty.

## Measurements

For every run capture:

- factual-answer correctness;
- authority-rule correctness;
- source precision, recall, identifier correctness, and revision correctness;
- schema validity and tool validity;
- blinded utility score for Generate;
- pre-provider source digest equality;
- time to first token, total latency, and tool latency, summarized as p50/p95;
- input, cached, reasoning, output, and tool token categories when reported;
- provider charges, retry charges, and total USD using dated prices;
- errors, refusals, retries, and terminal state;
- exact bytes, characters, and public source identifiers transmitted; and
- documented training use, retention, application state, region, and exceptions.

Do not infer unreported token categories or privacy guarantees. Mark them
unknown and include the provider evidence captured on the execution date.

## Selection rule

1. Disqualify every configuration with a boundary breach.
2. Compare surviving configurations in the measured table without a hidden
   composite weight.
3. Select only if one survivor dominates the decision-relevant measurements or
   the Warden explicitly supplies priorities that decide a documented tradeoff.
4. If the evidence shows a tradeoff, return the measured table and request the
   priority decision.
5. If neither provider passes, make no selection and revise the adapter or
   model candidates before another authorized run.

## Result template

```text
Execution date:
Harness commit and version:
Fixture version, base revision, and manifest/source digests:
Authorization reference and maximum spend:
Exact model pair authorized:

Provider:
Exact model/version:
API and version:
SDK and version:
Region and storage mode:
Request settings and output cap:
Pricing evidence URL and checked date:
Privacy/retention evidence URLs and checked dates:

Runs planned/completed:
Retries and reasons:
Hard disqualifiers observed:

Task results:
- factual correctness:
- authority correctness:
- source precision/recall/revision:
- schema/tool validity:
- blinded generation utility:

Usage categories:
Charges and total USD:
TTFT/total/tool p50 and p95:
Errors/refusals/terminal states:
Exact bytes/characters/source IDs transmitted:
Training/retention/application-state/region/exceptions:

Sanitized event artifact digests:
Raw request/response artifact digests:
Harness and report digests:

Decision: selected / tradeoff requires Warden decision / no selection
Rationale:
Open limitations:
```

## Official evidence to refresh at execution

These primary sources were checked on 2026-08-11. Their claims, models, prices,
and retention terms can change, so the executor must refresh and date them
without copying today's prices into the protocol.

### OpenAI

- [Responses streaming events](https://platform.openai.com/docs/api-reference/responses-streaming)
- [Function calling](https://platform.openai.com/docs/guides/function-calling)
- [Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [Data controls by endpoint](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [How API data is used for model improvement](https://help.openai.com/en/articles/5722486-api-data-usage-policies)
- [Models](https://platform.openai.com/docs/models)
- [API pricing](https://developers.openai.com/api/docs/pricing)

OpenAI's current official evidence says API inputs and outputs are not used for
training by default, while endpoint-specific abuse-monitoring and application
state retention vary. The execution report must record the exact endpoint,
settings, eligibility, and exceptions rather than abbreviating that distinction.

### Anthropic

- [Tool use with Claude](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)
- [Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [Commercial data retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)
- [Commercial model-training policy](https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training)
- [Pricing](https://docs.anthropic.com/en/docs/about-claude/pricing)

Anthropic's current official evidence says commercial inputs and outputs are
not used for training by default and documents standard API retention with
exceptions. The execution report must record the selected product, model,
workspace terms, storage controls, region, and any exception.

## Deliverables of an authorized execution

- Reproducible harness and synthetic fixture.
- Dated provider/model/API/SDK and pricing/privacy evidence.
- Sanitized raw event artifacts and digests.
- Per-run results plus p50/p95 aggregates.
- Usage and USD reconciliation within the approved maximum.
- Disqualifier report and selection result under the rule above.
- Public-safe summary containing no credentials, real campaign content, raw
  conversation material, or unsupported provider claims.
