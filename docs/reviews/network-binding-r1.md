# PR 87 network binding independent review, round 1

Reviewed source: `fa4d762f5f2eaade02fdb066a5e011de8982f5eb` on
`fix/executor-network-binding`.
Accepted base: `38ee11830d5e3dbf3e5c6f23da133322b71ac198`.
Review date: 2026-11-16. This report changes documentation only.

## Findings

No P0, P1, or P2 findings in the reviewed diff. The change is suitable to integrate
at this exact revision after required CI succeeds.

## Scope and reasoning

Read AGENTS.md, docs/isolated-executor.md, the complete accepted-base-to-source
diff, and surrounding runtime launch/reconciliation, qualification, proxy,
firewall, profile, harness, installed-artifact, and CI code.

- The authorization binding retains every explicitly configured deny address,
  including private addresses, and discovered global host IPv4 addresses. Private
  bridge churn therefore stops changing an otherwise identical authorization
  policy. Configured deny changes and global host address additions/removals still
  invalidate qualification. Source hashing also invalidates old proofs.
- Enforcement still receives `current_denied_ips()`, including the complete
  discovered host set. The unchanged proxy rejects any non-global or multicast
  IPv4 DNS answer and explicitly denied public host addresses, then connects to
  the validated numeric destination. This preserves private, CGNAT, loopback,
  metadata, reserved, and host-public denial. Direct DNS/host/public traffic and
  IPv6 remain governed by the unchanged task-network firewall.
- The new recipe has an independent profile ID and fixed isolated-Python argv;
  its complete profile differs from every smoke exemption. CI registers it before
  qualification. Its real database query precedes the started marker, and the
  probe requires unchanged binding/readiness before launch, during the private
  network/database lifetime, after service restart, and after cancellation.
  Restart/cancel preserve the container ID; cancellation confirms both containers
  absent, and export verifies the task-created marker.
- Qualification version 2 still requires all eleven existing checks. The ordinary
  lifecycle is additional actual workflow evidence, with failure-stage snapshots
  uploaded even when its strict assertions fail.

## Independent validation

Used a fresh full clone at the exact reviewed source, a reviewer-owned image,
private Docker config, private executor journal, and only that journal's labeled
resources. No live services, production database, host authentication settings,
native coding CLI, model credentials, or paid model calls were used.

Local environment: Python 3.12, rootless Docker 29.9.3, cgroup v2.
Task image:
`sha256:a14f9f0109a804bc87e6ac19f2c092a57351ee14a84a45af7e513c0b5c18f0e9`.
Disposable database image:
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
Executor source binding:
`81ad96048604b975225eeeb393cbdfcc7f1f88a5301d00183fa828c52528f533`.

| Check | Result |
| --- | --- |
| Executor unittest discovery | 73 passed |
| Black on changed Python files, Ruff on executor, mypy on executor | Passed |
| Full actual rootless qualification with ordinary profile registered | All 11 checks passed |
| Actual rootless ordinary database/restart/cancel/export lifecycle | Passed; all four snapshots qualified with identical binding |
| Ordinary recipe with qualification evidence temporarily absent | Rejected before launch; no launch intent or container |
| Completed reviewer executor resources | Zero containers and networks remain |

Local evidence is `/tmp/vibearr-review-network-binding-r1-boundary.json`,
`/tmp/vibearr-review-network-binding-r1-operation.json`, and
`/tmp/vibearr-review-network-binding-r1-negative.json`.

Independently checked the head SHA, successful steps, complete log, and downloaded
artifacts of the exact source's
[rootful Executor boundary run](https://github.com/innateessence/nightly-queue/actions/runs/43859347889).
It passed all eleven checks, the ordinary lifecycle, and the separately installed
executor service/client exercise outside the checkout. Its ordinary artifact shows
`172.18.0.1` appearing while active/restarted and disappearing after stop, while
every stage remains qualified with binding
`5c8d72606d31cc1f5fe23b92d2d6561731cbfd48f6cffccf5699c7125084d982`.
The same task container survives restart/cancel; the database exists while active,
and verified export includes `started.txt` with confirmed descendant termination.

The earlier diagnostic-only run's downloaded ordinary artifact was also inspected:
its sole newly discovered host address was `172.18.0.1`, and its active-stage
qualification changed to false. The final rootful evidence exercises that actual
topology regression rather than relying on rootless behavior.

Both required checks succeeded on exactly the reviewed source: the Executor
boundary run above and
[Tests](https://github.com/innateessence/nightly-queue/actions/runs/43859347871).

## Limits

Local rootless networking does not add its private gateways to the host FIB;
rootful topology behavior is supported by the independently inspected exact-head
GitHub Actions run and artifacts. The installed-artifact exercise was verified
in that CI run rather than repeated locally. The complete application suite was
not rerun locally; its exact-head Tests workflow passed in CI.
This review does not attest a native coding session or later MVP delivery/release
acceptance.
