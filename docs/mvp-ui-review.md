# MVP UI review

Branch: `feat/mvp-operations-ui`. This branch changes the web presentation, its
read-only queries and UI tests. It does not change execution, delivery, worker,
credential or queue mutation contracts.

## Behavior

| Page | Behavior |
| --- | --- |
| Queue | All four status filters; status tabs preserve search, priority and project; clearing filters retains status; All work removes only status. Counts in tabs reflect the other search filters. Global executable/queued counts remain independent. |
| Activity | Active attempts with saved stage, wait, heartbeat, repair count, validation and latest progress. Manual refresh reads new reports. Unknown readings stay unknown. |
| History | Paginated attempts with selected state retained, outcomes, validation, errors and saved PR links. Active attempts remain available in history. |
| System | Saved processing/schedule settings, connection count, issue sync timestamps, and redacted storage/gate reports with existing stale-report warnings. Links lead to the existing configuration forms. |
| Attempt detail | Existing CSRF-protected cancel/retry forms and evidence, accessible from queue cards and the operations pages. Historical approval records remain readable; the approval inbox is removed from navigation. |

No page enables processing automatically. The Activity page shows bounded saved
progress, not a live private-log stream. New priority/hold mutations and the full
feature 10 dashboard remain later work. GitHub-backed issue authoring is feature 11.

## Browser evidence

These are real Chromium renders against disposable PostgreSQL fixtures. The
example tasks, CI wait and PR link are demonstration data, not evidence of actual
coding, CI, delivery or merging. Implementing the status-filter UI manually does
not satisfy the MVP's autonomous two-issue acceptance scenario.

| View | Screenshot |
| --- | --- |
| Queue, desktop | [PNG](screenshots/mvp-ui/queue-desktop.png) |
| Queue, 390px mobile | [PNG](screenshots/mvp-ui/queue-mobile.png) |
| Activity | [PNG](screenshots/mvp-ui/activity-desktop.png) |
| History | [PNG](screenshots/mvp-ui/history-desktop.png) |
| Attempt detail | [PNG](screenshots/mvp-ui/run-detail-desktop.png) |
| System, mobile dark theme | [PNG](screenshots/mvp-ui/system-mobile-dark.png) |

The five browser tests in `work_queue/browser_operations.py` cover filter/clear
navigation, configuration → queue → activity/history navigation, operator forms,
desktop/mobile overflow, dark mode and JavaScript errors. Run them explicitly in
a Playwright 1.58.0 container against the disposable preview. They do not run in
normal Django discovery and do not add a production dependency.

`work_queue/browser_preview.py` seeds the isolated database and serves the preview;
it refuses a database other than `vibearr_ui` on `vibearr-ui-check-db`. Use a new,
empty Docker test database for each fixture setup. The preview and browser share
a private Docker network with no published ports, host mounts, engine socket or
production credentials. Capture screenshots inside the browser container and
export the selected files explicitly with Docker copy.

## Validation

- Five Chromium browser tests passed against the candidate pages.
- Final application regression results are recorded in [plan status](plan-status.md).
- Black, Ruff, mypy, Django system checks and migration drift checks run in Docker.
- An initial unrestricted 2,003-test discovery run terminated with signal 15 before
  completion; it is not counted as a passing full-suite result. The application
  suite is run separately from portable runner/executor tests for this UI branch.
