# Runner control API

The queue HTTP API and machine control API use separate bearer credentials.
`VIBEARR_API_TOKEN` is the generic queue token. A provisioned runner identity uses
its dedicated token; it cannot authenticate generic queue calls, and the generic
token cannot claim owned runner work or retrieve execution credentials.

All machine paths begin `/api/v1/runner/`. GET identity/configuration/readback
calls are observations; they cannot claim or start work. Claims, controller
binding, immutable artifacts, native dispatch/status, delivery and completion
have their own authenticated POST contracts and idempotency identities.

| Path family | Responsibility |
| --- | --- |
| `identity/`, `configuration/` | Scoped identity, capabilities, frozen configuration inputs |
| `runs/claim/`, `runs/active/`, `runs/<UUID>/` | Claim intent, current ownership and reconciliation |
| `runs/<UUID>/claim/` | Recover an existing owned claim token |
| `runs/<UUID>/progress/`, `artifacts/`, `evidence/` | Versioned progress and immutable evidence |
| `runs/<UUID>/finish/` | Complete only the currently owned attempt with checked delivery evidence |

Persist a run UUID before claiming. Repeating that UUID returns its original
attempt or empty observation; it never selects new work. Use a new UUID for a new
poll. Only claim/recovery responses expose the active claim token. Ordinary
readbacks omit tokens, digests and secret payloads. The server enforces one active
owned task and rejects foreign, stale or conflicting writes.

Ownership is bound to a private controller secret and durable local journal.
Immutable contexts, plans, native operations and factory artifacts retain their
original run/configuration/candidate identities. A plan is task data, not authority
to change tools or permissions. New source needs new trusted validation/review
evidence before native delivery can be marked ready.

Native success requires protected actual execution/termination receipts, current
source/export checks, registered worker profiles, qualified roles and verified
Git/PR delivery. Client-supplied summaries, public/redacted display documents or
agent-created URLs cannot grant success. Read complete paginated artifacts and
original authenticated worker statuses for recovery, rather than treating a
truncated inline history as complete evidence.

Token rotation/revocation does not stop external work or release its fence.
Reconcile and confirm original termination before changing ownership or retrying.
See [credentials](runner-credentials.md), [settings](runner-settings.md),
[recovery](runner-recovery.md) and [factory acceptance](factory-acceptance.md).
