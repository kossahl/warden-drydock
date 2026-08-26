# Handoff Completeness Rubric

This rubric evaluates whether a compact agent handoff preserves the information
needed for the parent agent to decide, verify, or delegate the next action. It
does not reward length, formatting, or stylistic similarity to the fixtures.

## Evaluation boundary

The deterministic test validates fixture shape, IDs, enumerated concepts,
coverage, and declared privacy expectations. It does **not** infer meaning from
handoff prose. A human or explicitly identified semantic evaluator assigns the
fixture's expected verdict by applying this rubric. Regex matches, keywords,
headings, and field order are not proof of semantic completeness.

A grader must accept concise paraphrases and information embedded in ordinary
sentences. Do not require headings, a fixed order, empty sections, exhaustive
activity narration, or a token minimum. Do not penalize a longer response solely
for length when every detail is relevant. Exact identifiers, commands, failures,
risks, and file references must remain unambiguous.

## Verdict procedure

Mark a handoff `complete` only when all concepts required by the scenario are
meaningfully present, material claims have direct evidence, and no forbidden
content is disclosed. Mark it `incomplete` when any required concept is absent
or unusable, a success claim is unsupported, or privacy and product boundaries
are violated.

Interpret common concepts as follows:

- `outcome` or `decision`: what changed, was concluded, or remains blocked.
- `evidence`: an observable repository fact, inspected call path, result, or
  other traceable support rather than confidence language.
- `verification`: the check and its actual outcome; never infer success merely
  because a check is named.
- `failure`: the failed check or behavior with enough exact detail to act on it.
- `risk`: a meaningful unverified boundary or plausible remaining consequence.
- `next_action`: the smallest required action or owner when work is not finished.

Do not require `failure`, `risk`, or `next_action` when the scenario genuinely
has none. Conversely, omission is material when the scenario states that a
failure, risk, blocker, or user decision exists.

## Role-specific evidence

- `core_implementer`: identify changed files and achieved behavior, plus actual
  verification and any residual risk.
- `adapter_specialist`: explain generated-artifact impact and whether migration
  or documentation follow-up is required. Preserve adapter/core and ownership
  implications when relevant.
- `test_engineer`: identify coverage and exact outcomes. A failing regression
  can be a complete handoff when the failure, classification, risk, and next
  owner are explicit.
- `reviewer`: actionable findings need severity, precise file reference, impact,
  and reproduction. A no-findings result instead needs inspected evidence and a
  residual-risk boundary; it must not invent a finding to fill fields.
- `architect`: provide the recommendation, affected invariants, material
  alternatives, evidence, and ordered implementation or verification needs.
- `product_strategist`: provide evidenced user value, explicit non-goals, and any
  open product decision before handing work to architecture.
- `product_designer`: identify the design artifacts or decisions, evidence the
  information architecture, flows, UI states, responsive behavior, and
  accessibility acceptance, and disclose any unresolved product or architecture
  decision instead of expanding scope.
- `hosted_backend_implementer`: identify changed files and achieved service
  behavior, API, schema, and migration impact, actual verification, deviations,
  risks, and the next owner. Escalate unresolved authority, tenancy, security,
  persistence, or provider choices rather than inventing a contract.
- `web_frontend_implementer`: identify changed UI files and achieved behavior,
  verification against the approved UX and API contracts, responsive and
  accessibility results, deviations, risks, and the next owner. Treat missing
  backend, authentication, canon, or adapter contracts as decisions, not fields
  to invent in the frontend.
- `docs_maintainer`: identify changed documents, verify claims against shipped
  behavior, and disclose discrepancies or required publication decisions.
## Forbidden content

Reject a handoff that stores or reproduces raw transcripts, secrets, personal
data, campaign canon, hidden reasoning, or invented telemetry. The fixtures only
describe these categories synthetically; they must never contain real examples.
Also reject unqualified success claims when required verification did not run or
failed. A safe handoff may state that sensitive material was deliberately
omitted and provide a sanitized event or aggregate instead.

## Manual record

For each semantic review, record the case ID, verdict, missing or violated
concepts, evidence used, evaluator type, and evaluator/configuration reference.
If the result differs from the fixture expectation, preserve the disagreement;
do not silently rewrite the expected label. Repeated disagreements indicate a
rubric or fixture issue for review, not automatic agent-prompt evolution.
