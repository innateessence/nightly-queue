# Delete through the UI

Queue rows, projects, credentials, coding runners, and schedules have a Delete
link. Each opens a confirmation page with the affected object, consequences,
Cancel, and a CSRF-protected Delete button. Opening a page never deletes anything.

- Deleting a coding runner removes its configuration and disables its assigned
  machine identity. The identity and run history remain. An identity with active
  work must finish or be stopped and reconciled first. The built-in default runner
  is the required fallback and cannot be deleted.
- Deleting an assigned schedule requires choosing another schedule for all its
  runners. Their other settings stay intact; the new schedule applies at their
  next processing gate. The built-in Always on schedule is required for new
  runners and cannot be deleted.
- Deleting a credential removes current selections and preflight grants. Runners
  selecting it are disabled and paused. Projects selecting it for imports have
  automatic discovery disabled, preventing fallback to another import account.
  Explicit project runner overrides are cleared. Active runs using it block
  deletion. If retained runs reference it, access is revoked and encrypted
  material and rotations remain available only for history redaction. Otherwise
  its secret and saved versions are erased. A deleted name can be reused. Remote
  provider credentials are not revoked by this action.
- Deleting a project stops discovery, removes its runner overrides, and removes
  its work from the queue. Running work must first be stopped and reconciled.
  Project and item records needed by run history remain readable through history.
  The remote repository and issues are unchanged.
- Deleting a work item removes it from queue views and claims, including imported
  issues and work with finished attempts. Running work cannot be deleted. An
  imported issue stays open remotely; a local marker prevents later syncs from
  adding it again. Deleted items cannot be retried from retained run history.

Removed projects and work items use deletion timestamps. Normal model queries,
forms, APIs, configuration snapshots, and discovery exclude them. Historical
foreign keys use the unfiltered base manager. Deleted credentials are excluded
from normal queries and secret delivery; redaction explicitly includes retained
credential material. Existing ORM hard-deletion protections are preserved.
Deletion shares the existing controller, configuration, project, and claim locks.
Stale claims and saves cannot restore deleted records.
