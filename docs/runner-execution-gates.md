# Processing schedule and allowance gates

`coding_runner/gates.py` is a standard-library policy evaluator. The controller
persists and publishes a report on every normal poll. Startup `--check` stays
read-only. There are no live allowance adapters and the configuration's
`execution_ready` remains false. Neither a green policy report nor changing
`enabled` authorizes live execution.

## Runtime contract

Construct `GateEvaluator(journal, clock=aware_datetime_callable,
adapters={(backend, credential_id): adapter}, max_usage_age_seconds=300)` under
the existing exclusive journal lock. The default clock is UTC. Call
`evaluate(configuration, state, checkpoint, run_id=None, active_count=0,
capacity=1)` before a claim, before each stage, before each model invocation,
and periodically during backend calls that support safe interruption. Checkpoints
are `claim`, `stage`, `model`, and `periodic`. The result has `allowed`, `reasons`,
`next_check_at`, and a JSON-safe `data()` representation. Exceptions deny work;
callers must never treat evaluation/transport failures as permission.

Fetch the current configuration document for every checkpoint. Global pause,
schedule, reserve, limits, and account inventory apply immediately at the next
check; backend/model/instructions still come from the run's frozen snapshot.
Original participating accounts are also retained at claim, so editing selections
cannot drop an in-flight account out of reserve enforcement. Current and original
accounts are checked as a union; no adapter/account fallback is permitted.
The one-controller, one-task capacity remains the initial supported deployment.

Before sending a claim, call `prepare_claim(state, run_id, decision)` to persist
its interval and account inventory. It rejects blocked, expired, superseded, or
unreconciled decisions. Include the current configuration revision in the claim
API request, and use the operation journal around the mutation. Only after a
confirmed active claim, or reconciliation proving the original claim active, call
`record_claim(state, run_id)`. It charges that attempt once, retaining its original
interval even when reconciliation crosses midnight, and consumes the checkpoint
so another claim requires fresh ownership and limit checks. A confirmed empty/not-found
outcome can call `abandon_claim`; ambiguous responses cannot. A resume never
charges again. Counters and attempt identities survive process restart.

An owned run blocks all new claims, including while waiting. Checkpoints must
match the owned run. A remote active run with missing local claim accounting
blocks until recovery establishes the original claim; it cannot silently resume
and lose the task limit. Call `complete_run` only after remote completion has been
confirmed. Gate waits retain claim/workspace/plan, and persist the checkpoint,
reasons, first wait timestamp, and accumulated paused seconds. `paused_seconds`
includes an open wait and allows active stage/task timeout accounting to subtract
waiting time. Safe interruption, timeout budgets, progress-stage restoration,
and cancellation are responsibilities of the stage/recovery controllers. A blocked
periodic check requests a graceful checkpoint; this reserve cannot promise an
exact cutoff for an already running provider call.

## Schedule policy

Weekdays are Monday=0 through Sunday=6. An overnight window belongs to its
starting weekday. Boundaries are inclusive at opening and exclusive at closing.
A nonexistent local boundary shifts forward to the first valid local minute.
Repeated openings use the earlier instant; repeated closings use the later one.
All comparisons and durations use UTC, including folds and DST transitions.

Overlapping and touching windows form one processing interval. Its stable ID is
a SHA-256 of timezone and the merged UTC start/end, independent of window order,
duplicate rows, poll time, and restart. Finite weekly components are evaluated
with an expanding search until both component boundaries are complete, including
when a DST gap removes the only weekly closure and joins adjacent weeks. If the
weekly union has no gap, it uses the continuous-mode local-calendar-day policy;
it never derives an identity from a moving lookback. Continuous days use the
`day:<timezone>:<local-date>` identity and can last 23 or 25 hours at DST changes.
A new day resets eligibility for a new claim, while an existing attempt retains
its old identity. Editing a schedule can create a different interval; editing
pause, task limits, reserve, model, or window ordering does not reset its count.

Reports contain the current interval, count, next opening/closing boundary, and
next recheck deadline. The deadline is the earliest poll, schedule boundary,
usage freshness expiry, or reported provider reset. The controller honors that
deadline even with a long configured poll interval. Provider reads that take
longer than the checkpoint deadline block with `checkpoint_expired`; a slow
response cannot return permission after a closing or freshness boundary.

## Allowance adapter contract

An adapter implements `read_usage(account, now) -> UsageReading`.
`UsageReading(supported, windows)` contains a tuple of
`UsageWindow(window_id, remaining_percent, observed_at, reset_at=None,
unit="percent")`. This is an allowance percentage supplied by a supported
provider integration, not a computed task token balance. Stable nonsecret window
IDs and aware timestamps are required. A provider must enumerate **every**
applicable allowance window; unknown/unavailable windows must not be omitted to
make a result look usable. Account identity remains `(backend, credential_id)`;
selected override accounts are already included by the configuration endpoint.
Real adapters need independent capability/provider evidence before release.

When enforcement is enabled, every account and every window must be strictly
above the single global threshold. Exactly 40% blocks a 40% reserve, 0% blocks
an enabled 0% reserve, and 100% reserve always blocks. Percentages are never
averaged. Missing accounts/adapters/windows, unsupported reporting, exceptions,
invalid/nonfinite values, duplicate window IDs, future observations, readings
aged **300 seconds or more**, or readings whose reset has passed all block.
A reset time alone does not establish renewed allowance; a fresh reading does.
Provider exception text is never included in evidence. Enforcement disabled
removes this usage gate; it does not remove schedule, pause, capacity, ownership,
or release checks.

## Display evidence API

`POST /api/v1/runner/gates/report/` accepts `{"report": decision.data()}` under
the provisioned runner bearer token. Unlike per-run progress it also works before
a claim exists. Run-specific reports require an owned active run. The report is
strictly allowlisted, limited to 64 KiB, 128 account readings and 32 windows per
account, with finite bounded numbers and aware timestamps. It contains no claim
or credential secret fields. Shared JSON request validation rejects null characters,
unpaired Unicode surrogates, nonfinite floats and nesting beyond 20 levels before
database writes, preserving valid Unicode and literal JSON escape text.
A timestamp-identical retry is idempotent; changed
content at the same timestamp conflicts. Older retries cannot replace newer
evidence. This latest-report write is informational and does not authorize any
queue transition or external operation.

`GET /api/v1/runner/gates/` returns schema version 1 and the authenticated runner's
latest `evidence` (`report`, server `received_at`) or null. The Settings page
shows each runner's reasons, threshold/enforcement, account/window percentages,
observation/reset timestamps, interval/count, and next boundary. It flags expired
reports and reports made before current configuration changes. HTML escapes
provider identifiers. To retain per-run history and update the waiting stage,
controllers additionally use the existing retry-safe run progress/artifact API;
latest evidence alone does not replace durable run events.

Migration `runner_control.0003_gate_evidence` adds one latest-evidence row per
runner. Apply normal migrations before deploying the updated poller. The UI
screenshot uses fake allowance evidence and demonstrates no live provider probe.

## Verification

`python -m unittest coding_runner.test_gates` covers schedule boundaries, midnight,
DST folds/gaps, overnight/weekly overlap, continuous week/day policies, limits,
restart accounting/waits, capacity, pause/release/ownership checks, reserve
0/40/100, multiple accounts/windows, overrides, original account retention,
missing/stale/unsupported/invalid readings, resets, deadlines, and controller
publication without claims. `python manage.py test runner_control.test_gates`
covers authentication/isolation, durability/retries, bounded secret-free schema,
settings evidence, expiry, and changed configuration. All provider inputs are
fake; controlled live release acceptance remains outstanding.
