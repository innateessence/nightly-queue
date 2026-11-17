# vibearr plan status

Snapshot: 2026-11-16. This is a point-in-time overview, not a live status page.
UI work branch: `feat/mvp-operations-ui`, pushed for review in [PR #134](https://github.com/innateessence/nightly-queue/pull/134).
No merges or runtime changes are part of this branch.

The core automation code is merged. The remaining MVP work is making the entire
loop Docker-only and proving it works end to end.

“Implemented” below means code exists, not that it meets the revised Docker-only
release requirements.

| Feature / area | Status | What remains |
| --- | --- | --- |
| Docker-only plan and README | 🟡 PR open | Merge [#133](https://github.com/innateessence/nightly-queue/pull/133) |
| **00 — Rename to vibearr** | ✅ Merged | Compatibility naming retained |
| **01 — Remove approval gates** | ✅ Merged | Recheck during Docker acceptance |
| **05 — GitHub work selection** | ✅ Merged | Owner filtering, priorities, holds and dependencies implemented |
| **02 — Agent isolation** | 🟡 Replacement needed | Replace host executor with Docker worker slots |
| **03 — Native Codex** | 🟡 Implemented | Adapt and verify against the new workers |
| **15 — Independent validation** | 🟡 Implemented | Move validation onto the new Docker boundary |
| **04, 06, 07 — Issue → coding → PR → merge** | 🟡 Implemented | Integrate Docker replacement; prove the complete unattended loop |
| **D1 — Docker packaging** | 🔴 Not implemented | Complete Compose stack, immutable images, private volumes and networks |
| **D2 — Socketless workers** | 🔴 Not implemented | Container workers, isolation, cleanup, cancellation and restart recovery |
| **D3 — Docker validation** | 🔴 Not implemented | Trusted checks, isolated test databases and dependency artifacts |
| **D4 — Docker operations and acceptance** | 🔴 Not implemented | Containerized setup, CI, backups, recovery and real two-issue proof |
| **08 — Native Claude Code** | 🟡 PR open | Finish [#108](https://github.com/innateessence/nightly-queue/pull/108); verify Docker compatibility |
| **09 — Native OpenCode** | 🟡 PR open | Finish [#109](https://github.com/innateessence/nightly-queue/pull/109); verify Docker compatibility |
| **MVP UI — status filters and run visibility** | ✅ Implemented and verified on UI branch | 984 application tests completed (6 skipped), 5 browser tests passed. Ready for review; not merged. |
| **10 — *arr dashboard** | 🟡 Initial views on UI branch | Queue, Activity, History and System views added. Full live-log streaming and new priority/hold controls remain later work. |
| **11 — Create work through the app** | ⚪ Planned | Web-created GitHub issues and bounded agent follow-ups |
| **12 — Automatic self-updates** | ⚪ Later | Deploy upgrades and recover entirely within the Docker boundary |
| **13 — Multiple project workers** | ⚪ Later | Bounded parallel execution |
| **14 — Remove legacy infrastructure** | ⚪ Later | Delete obsolete approval/proposal code after migration |

Next sequence: merge the plan → **D1 → D2 → D3 → D4** → demonstrate issue A
merging automatically, followed by dependent issue B using A’s changes.

The [Docker-only plan PR](https://github.com/innateessence/nightly-queue/pull/133) defines those four branches.
Autonomous execution stays disabled until they qualify.

See the [full development plan](autonomous-ai-queue-plan.md) for individual feature
branches and acceptance criteria.

## UI branch scope

- Add all four queue status filters, preserve search/project/priority when changing status,
  and keep the selected status when clearing search filters.
- Surface the latest attempt from a queue item and show existing stages, waits,
  heartbeat, repair count, validation, errors and PR links in Activity and History.
- Put saved processing configuration, connection counts, synchronization and redacted
  runner storage/gate reports on System. Show missing reports as unknown.
- Remove approval inbox navigation; retain historical records and existing APIs.
- Keep cancellation/retry and configuration changes on their existing CSRF-protected
  endpoints. Do not implement workers, change execution policy, enable processing,
  deploy, merge PRs or make agent calls.

## UI validation

- Docker application suite: 984 tests completed, 978 passed and 6 skipped. Command:
  `python manage.py test credentials projects work_queue runner_control vibearr_app nightly_queue --noinput`
  inside the isolated test container with disposable PostgreSQL.
- 21 new Django UI regression tests are included in that suite.
- Five Playwright/Chromium tests passed, including desktop/mobile screenshots,
  dark theme, status/filter navigation and existing operator forms.
- Black, Ruff, mypy, Django system checks and migration drift checks passed in Docker.
- The initial unrestricted 2,003-test run terminated before completion; it is not
  counted as a passing full-suite result.
- [UI review and screenshots](mvp-ui-review.md) describe the behavior and evidence.
  Screenshots use disposable fixture data and do not prove autonomous delivery.
- Docker corrections D1–D4 and real two-issue acceptance remain pending. Processing
  was not enabled, no model calls were made, and nothing was merged or deployed.
