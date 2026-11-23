# Factory settings and review routing

Factory execution is disabled by default. Existing native configuration continues
to work without role capabilities. Enabling factory execution requires qualified,
boot-bound worker profiles; a setting does not grant tools or establish readiness.

Use the project's existing runner override form to replace `Factory` with JSON:

```json
{
  "schema_version": 1,
  "enabled": true,
  "max_repairs": 2,
  "roles": {
    "reviewer-code": {
      "model": "gpt-5.5",
      "instructions": "Check the acceptance criteria and changed tests."
    }
  }
}
```

Select a model supported by the configured native provider. Roles inherit the
implementation backend, selected connection, model and instructions unless their
model/instructions are replaced. Role settings cannot select another account,
backend, executable, network policy or tool grant. The resolved configuration and
existing credential metadata are frozen in the run's claim snapshot. Editing an
override invalidates preflight's revision and applies to later attempts.

Code review always runs. Trusted changed paths and intake surfaces add security
or both UX/UI reviewers. Renames inspect both old and new paths; deletions retain
their affected path. Unknown changes require every registered reviewer at high
risk. Path rules may add reviews but cannot remove the built-in safety routes or
protected validation paths. A prior round's requirements remain when routing a
changed candidate under the same frozen settings.

Built-in security routes cover root and nested authentication, credential,
security and middleware modules; trusted runner/control/app code; queue authority;
and dependency manifests. Ordinary project model code can still require only code
review. Replacement rules cannot remove these security routes. The 100-entry
rule/protected-path limits include required defaults, so accepted frozen settings
remain valid when normalized again.

Trusted Git diff callers must retain both sides of renames, for example with
`git diff --name-only --no-renames -z <base> <head> --`, and pass deleted paths to
the router. Plain `--name-only` with rename detection can omit a security or UI
source path; the router cannot reconstruct a path the caller discarded.

Routing records contain rule, path-inventory and factory-settings digests. The
factory lifecycle persists them as immutable role artifacts bound to the current
run, specification and candidate; the settings digest does not replace the full
configuration-snapshot digest. New candidates need fresh validation and reviews.
Missing qualified role/browser capability prevents the corresponding dispatch;
model claims and advertised booleans do not substitute for worker-boundary proof.

Protected-path changes remain subject to the trusted validation-profile and
separate qualification policy. Routing marks them for security review; it cannot
authorize replacing checks. Factory repairs are capped by the existing execution
policy. Every PR still requires a human merge.
