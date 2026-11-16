# Feature06 queue observations

The [queue record](feature06-queue.json) preserves two real runs in the owned
disposable environment. It is partial acceptance evidence. It does not claim
successful CLI delivery or completion of the MVP.

Selected Codex 0.169.0 authenticated with the explicitly saved session account and
model `gpt-6.1-sol`. This was a non-refreshing selected-session test, not an API-key
live test. Scheduled Celery discovery imported both owner-created issues; no
manual queue insertion or approval record was used.

| Case | Discovery | Agent start | Result |
| --- | --- | --- | --- |
| [117](https://github.com/innateessence/nightly-queue/issues/117), deliberate regression | 73.965 seconds | 148.872 seconds | Independent Django check exited 1; no publication |
| [119](https://github.com/innateessence/nightly-queue/issues/119), instructions file | 33.878 seconds | 123.485 seconds | Source refresh rejected before validation; no publication |

Durations start at provider issue creation. Neither run produced a PR, so neither
has an issue-to-PR measurement. Both have protected termination evidence,
completion acknowledgements and released active slots. The same agent/container
was observed from independent supervisor processes. The counter remains 2 in its
original task window; the original cutoff and private journal were preserved.

Issue #117 used the frozen zero-repair policy. Black, Ruff, mypy, migrations and
system checks passed before Django failed the deliberately added test; remaining
fixed checks were canceled after failure. All ten registered task operations
terminated and 21 local processes stopped. The original supervisor was `74ca728`;
collector `5dd6cd7` subsequently read the same evidence after fixing epoch receipt
timestamps. The task was not rerun. Issue #117 was closed as `not_planned`, with no
success or merge evidence.

Issue #119 used the restored repair allowance 2. A source-refresh HTTP400 overlapped
a scheduled import while frozen/current issue revisions remained identical.
This supports a superseded-read race diagnosis; the exact response body was not
retained. Its agent was canceled, its operation terminated and 14 local processes
stopped. The separately merged [source-refresh fix](https://github.com/innateessence/nightly-queue/pull/122)
adds typed transient handling and bounded fresh collection while retaining source
authority fences. It does not change this historical failed outcome; this CLI
attempt was not repeated after the fix. Issue #119 remains open with
`vibearr:hold`; its body and failed history are unchanged. The version task was
never published.

The revised completion scope stops further CLI rehearsals. Feature07 owns the
stronger combined delivery proof using the reserved queue status-filter task and
dependent link/clear task: discovery, native edits, all nine independent checks,
CI, automatic main merges, restart reconciliation, dependent-base ancestry and
rendered screenshots. That proof remains separate from these historical failures.
The feature06 bootstrap code and deliberate negative case do not alone establish
successful automatic merging or a deployed MVP.
