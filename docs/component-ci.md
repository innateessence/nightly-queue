# Component CI with pinned prerequisites

The small factory PRs contain owned changes against an unmerged common base.
Their shared-file dependencies are not independent releases. The **Component
with pinned prerequisites** check tests the actual PR's complete changed-path
set together with an explicit, immutable complete-stack prerequisite source.
It does not claim standalone-head runtime or provider qualification.

The baseline is `b03776d52a43532e85b2dbca72d3ed7b58beec8e`; the prerequisite source
is `25a3c786d977facb304868dcfd94d3901a916df1`. Both commit and tree identities are
pinned. Each PR's `.github/component-prerequisites/pr-NUMBER.json` enumerates every
unowned prerequisite path, Git mode/type/blob, or deletion. This is the full
reviewed stack prerequisite set, not a minimal issue dependency graph.

Each PR uses its own path so unrelated manifest additions do not conflict when
humans merge dependency PRs. The workflow supplies the actual PR number and
component SHA externally; the wrapper selects exactly that canonical manifest.
Missing paths, another PR's path and the old shared filename are not fallbacks.
A Docker Git job starts
from that exact head, applies only the complete allowed unowned delta, and checks
the entire resulting tree. Every candidate edit, new file, deletion and executable
mode remains exactly as committed. A changed shared file is never replaced by
known-green donor bytes; repair it visibly on the PR and regenerate its manifest.
Missing, extra, duplicate, stale or tampered records fail the check.

`assembly.json` records the original candidate, immutable pins, all protected and
imported entries, and the actual two-parent derived commit/tree. Existing
`checks.sh full` runs unchanged against that **derived** commit, including unit,
static, packaging, four-service execution, browser and recovery gates. Artifacts
include composition provenance and the normal full-check evidence. Integration
PR #180 separately tests its own complete source directly.

All Git/Python and source assembly run inside bounded Docker jobs. The operator
shell copies only Git objects/ref metadata into an immutable input image; it
imports no host Git configuration, credentials or hooks. Offline assembly rejects
missing objects rather than using host tools or network fallbacks. Full-history
checkout must include the pinned source objects. Temporary source volumes and
containers are removed after the check; no runtime Docker socket is introduced.

Generate a branch manifest with the reviewed helper inside its Docker Git job:

```sh
python deploy/docker/component_ci.py manifest --repository /work/repo \
  --candidate ACTUAL_HEAD_SHA --write-manifest /work/repo/.github/component-prerequisites/pr-PR_NUMBER.json
```

Commit the generated manifest; it intentionally does not pin its own future head.
The workflow's external candidate SHA binds that final commit and the exact
manifest/tooling blobs captured for assembly. Human review and merging remain
required, and a successful composed check does not enable paid execution.
