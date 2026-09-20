# Hosted browser capture storage

The local pilot persists only live-session Capture operations in the browser.
Campaign snapshots, Canon, provider secrets, and workflow authority remain
server-side or in the immutable campaign revision model.

## Choice

| Mechanism | Survives reload/restart | Offline queue | Quota/error signal | Two-tab coordination | Decision |
| --- | --- | --- | --- | --- | --- |
| `localStorage` | Yes | Possible, but synchronous and small | Weak | Event-based only | Rejected |
| Cache API | Yes | Suited to request responses, not mutable records | Indirect | Weak | Rejected |
| IndexedDB | Yes | Transactional records and queues | Explicit request/transaction errors | Serialized read/write transactions | Selected |

IndexedDB is used behind `CaptureStore`, so the queue and server protocol do
not depend on browser-specific storage calls. A missing or full store fails
the capture instead of reporting false success; the caller can surface
**Needs attention**.

## Stored identity and recovery

One installation-scoped device ID and a monotonic device order are stored in
the same database as captures. Each capture is keyed by
`session/device/operation`, includes its campaign, pinned base revision,
controller binding, text, type, optional affected record, and payload digest,
and starts as **Saved on device**.

The sync queue sends captures sequentially. `accepted` and `exact_replay`
become **Synced**. A transient transport failure leaves the record pending;
an idempotency or binding conflict becomes **Needs attention** without
deleting the captured text. A crash after server acceptance is safe because
the next attempt replays the same operation identity and digest.

Ending a session persists an immutable operation set before it can be sent.
The end intent is withheld until every named capture is acknowledged, and it
is marked synced only when the server reports `ready_for_proposal`.
