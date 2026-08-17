# Provider bake-off evidence — 2026-08-17

## Outcome

No provider/model is selected. Anthropic was hard-disqualified; Terra versus Luna remains a Warden priority tradeoff.

## Measured comparison

| Model | Runs | Disqualified | Cost USD | TTFT p50/p95 ms | Latency p50/p95 ms | Blind Generate median |
|---|---:|---:|---:|---:|---:|---:|
| `claude-sonnet-5` | 12 | 1 | 0.077924000 | 2225.708/3643.024 | 5772.599/8052.803 | n/a |
| `gpt-5.6-luna` | 12 | 0 | 0.004439400 | 424.923/945.695 | 1953.682/3987.669 | 3.0 |
| `gpt-5.6-terra` | 12 | 0 | 0.041274000 | 412.877/764.272 | 1958.741/2752.795 | 3.5 |

All 36 corrected calls completed with no retries. Total measured spend was `$0.123637400`, below the `$5.00` hard cap.

The earlier invalid run is audit-only: 36 HTTP 400 responses, zero inference evidence, zero retries, zero tokens/model IDs, and `$0.00` spend.

## Decision gate

Choose the priority that resolves the surviving tradeoff: prefer Terra for the higher blinded Generate median and better p95 latency, or Luna for substantially lower measured cost with similar p50 latency.

## Evidence limitation

Per-run token categories are unavailable because the local sanitizer redacted keys containing 'token' after actual cost was computed. No token values are inferred; the sanitizer regression is fixed for future runs.

Machine-readable evidence digest: `7a45daf26feeb0d08da1cff807013426c05eef82b376b0231354ddffb144285f`
