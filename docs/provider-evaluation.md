# Provider evaluation: personal pilot decision

This record separates two conclusions. The controlled bake-off did **not** select a winner: `claude-sonnet-5` was disqualified, while `gpt-5.6-terra` and `gpt-5.6-luna` presented an unresolved quality/cost tradeoff under the precommitted no-hidden-weight rule. After reviewing that evidence, the Warden explicitly selected `gpt-5.6-luna` for the local personal Windows pilot because it was sufficient for the prototype and materially cheaper.

That product decision is intentionally narrower than the evaluation result. It does not establish Luna as the provider for a future public MVP.

## What was tested

The deterministic matrix used synthetic Erebos campaign data only. Each candidate ran each task three times, for 12 calls per model and 36 corrected calls total:

- **Ask - current airlock state:** answer the current state of the Erebos airlock and identify what changed. This tested grounded retrieval, source citations, current-state synthesis, and Draft-only authority behavior.
- **Check - Vale death claim:** determine whether the claim that Vale is dead is established when canon says no death is recorded, a preparation note predicts death, and a confirmed operation only records beacon activation. This tested authority ordering and resistance to treating predictions as facts.
- **Generate - infirmary details:** produce three sensory details from the sparse fact that the infirmary is powered and available. This tested useful creative expansion, grounding, Draft labeling, and unsupported embellishment risk.
- **Tool - beacon debrief:** emit a structured Draft proposal through the allowlisted tool contract. This tested strict schema compliance, source-set and revision binding, tool selection, and the prohibition on claiming that canon had been applied or promoted.

## Runs and audit history

An initial request-contract run produced 36 HTTP 400 responses, 12 per candidate. It reached no inference, returned no model identifiers or token usage, used no retries, cost `$0.00`, and is retained only as an unscored audit fact.

After deterministic contract corrections and an independent pre-spend review, the same matrix restarted from zero. All 36 corrected calls completed in 36 attempts with no retries. Measured spend was `$0.123637400`, below the `$5.00` hard cap.

## Measured comparison

| Model | Runs | Hard gates | Cost USD | TTFT p50/p95 ms | Latency p50/p95 ms | Blind Generate scores; median; range |
|---|---:|---|---:|---:|---:|---|
| `claude-sonnet-5` | 12 | failed: 1 disqualifying run | 0.077924000 | 2225.708/3643.024 | 5772.599/8052.803 | n/a |
| `gpt-5.6-luna` | 12 | passed | 0.004439400 | 424.923/945.695 | 1953.682/3987.669 | [3.0, 2.75, 3.75]; 3.0; [2.75, 3.75] |
| `gpt-5.6-terra` | 12 | passed | 0.041274000 | 412.877/764.272 | 1958.741/2752.795 | [3.25, 3.5, 3.5]; 3.5; [3.25, 3.5] |

Both OpenAI candidates passed every factual, grounding, authority, output-schema, and tool-contract hard gate. Terra's blinded Generate group had the higher median and tighter range. Luna cost `$0.004439400` versus Terra's `$0.041274000` - about 9.3 times less - while their measured p50 latency was effectively similar (`1953.682 ms` versus `1958.741 ms`). Terra had the better p95 latency (`2752.795 ms` versus `3987.669 ms`).

### Sonnet hard disqualification

`claude-sonnet-5` failed one of its three Generate repetitions. The response used the required object shape but returned an empty `details` array where the task required exactly three details. Deterministic validation therefore recorded `generation_contract_failure` and `invalid_output_schema`. Under the accepted protocol, one hard-disqualifying run disqualified the candidate; its other successful calls do not erase that failure.

### Representative sanitized outputs

These short excerpts illustrate the surviving candidates; they are examples, not an additional scoring pass:

> Luna, Ask: "The Erebos airlock is currently open. The change was that the crew opened it at 22:10 during the Relay breach, and it remains open."

> Terra, Ask: "The Erebos airlock is currently open. It was opened by the crew at 22:10 during the relay breach, and it has remained open since."

> Luna, Generate: "A low electrical hum suggests that the infirmary is powered."

> Terra, Generate: "A steady electrical hum underscores the powered infirmary."

The blind review found neither group clearly dominant. Terra was more consistently evocative and varied; Luna included the strongest individually grounded output. Some of Terra's apparent quality edge depended on provisional sensory additions such as antiseptic scent or glowing equipment that were not present in the source fact.

## Why the bake-off did not choose automatically

The protocol deliberately prohibited inventing weights after seeing results. Terra led the blinded Generate median and p95 latency; Luna led cost by a large margin with similar p50 latency. Turning those dimensions into a single ranking would require a quality-versus-cost weighting that had not been defined before inference. The evaluation therefore preserved the tradeoff instead of retroactively choosing a convenient formula.

## Product decision for the personal pilot

The Warden subsequently selected `gpt-5.6-luna` for the local personal Windows prototype/pilot. This is an explicit product-priority decision, not a rewritten bake-off result. Luna passed every hard gate, was judged fully sufficient for the prototype, had p50 latency similar to Terra, and cost about 9.3 times less in this run.

Scope boundary: this decision authorizes Luna for the personal pilot only. It does **not** establish Luna, OpenAI, or any other candidate as the provider for a future public MVP.

## Public MVP revisit

Before a public MVP chooses a provider, run a separate authorized evaluation using then-current model identifiers and pricing. The evaluation should include broader eligible candidates, more repetitions, and tasks representative of the public product. Candidate eligibility must account for API availability, privacy and retention controls, structured/tool support, operational fit, regional access, and a bounded cost envelope.

Before inference, precommit the ranking or weighting policy and the decision thresholds. The policy should explicitly cover quality, grounding fidelity, authority behavior, structured/tool reliability, latency, cost, privacy, and operational constraints. This record does not choose those weights; doing so belongs to the future decision gate, before results are known.

## Evidence limitations

- The sample is small: three repetitions per task and 12 calls per model.
- Per-run token categories are unavailable because the local sanitizer redacted keys containing 'token' after actual cost was computed. No token values are inferred; the sanitizer regression is fixed for future runs.
- The fixtures are synthetic and deliberately narrow. Results must not be generalized beyond this protocol.
- Latency and pricing are observations from this dated run, not durable provider guarantees.

## Evidence traceability

The complete sanitized run record is [`tests/provider_eval/evidence/provider-bakeoff-2026-08-17.json`](../tests/provider_eval/evidence/provider-bakeoff-2026-08-17.json). It was first committed at `f491b48cf4331e5adadc52c0b323c41b2c51a879`.

The machine-readable `decision` field and measured values retain the original evaluation conclusion. The distinct `post_evaluation_product_decision` field records the later Warden decision without changing that historical conclusion.

Measured-evaluation evidence digest: `7a45daf26feeb0d08da1cff807013426c05eef82b376b0231354ddffb144285f`
