# Troubleshooting

This guide assumes the automation is already configured as described in
[README.md](README.md). Start with the failing run's summary, then inspect the
relevant artifact and the durable state. Do not bypass a red gate by pushing
directly to `main`; fix the cause or merge through the documented human path.

## First checks

```bash
gh run list --workflow "Upstream sync" --limit 5
gh run list --workflow "Validate build" --limit 5
gh run view <run-id> --log-failed
gh pr checks <pr-number>
```

```bash
git fetch origin
git show origin/automation/state:state.json | head -100
```

The state branch is the source of truth for what was scanned, integrated,
deferred, or built. Workflow artifacts expire; the state branch does not.

## Upstream sync

### The run succeeds but there is no candidate PR

This is expected when every new upstream commit touches a protected or
review-only path, when there are no new commits, or when the candidate became
empty because its commits were already merged.

Check:

1. The run summary and the `upstream-sync-<run-id>` artifact
   (`change-report.json`, `change-report.md`).
2. Whether a review PR was created instead. Protected and mixed commits are
   listed there and are intentionally not in the candidate.
3. `last_scanned_sha` in `state.json`. A previous run may already have scanned
   the same target.
4. `candidate` in `state.json` and the open PR whose head is
   `automation/upstream-candidate`.

If the candidate became empty, the workflow closes the stale candidate PR and
deletes the branch. That is intentional.

### A commit was integrated before, but the next run tries it again

Check `integrated_shas` and `processed_shas` in `state.json`. The merge recorder
adds candidate SHAs to `integrated_shas` when a candidate PR merges. A review PR
moves pending SHAs to `deferred` unless the PR body lists `Upstream-SHA: <sha>`
lines. If you integrated code manually, add those lines to the review PR body
before merging so the next sync does not retry it.

### A trivial commit is stuck in review

Run the classifier locally against the two commits and inspect the reason:

```bash
python .github/scripts/ui_guard.py \
  --repo . \
  --base <parent-sha> \
  --head <commit-sha> \
  --report-dir ui-report
```

On Linux runners use `python3`. On Windows use `python`; the `python3` alias may
open the Microsoft Store.

Common causes:

- The commit touches `patch.sh` in a section declared `protected` or `review`.
- The commit touches an unclassified path; the default is `review`.
- The commit touches `build.sh`, `common.sh`, `.github/**`, `LICENSE`,
  `.gitmodules`, `res/**`, `fastlane/**`, `extensions/**` or `args.gn`.
- The commit message or body contains a security keyword; it may still be
  candidate-eligible, but its risk is high and auto-merge is blocked.

That is working as designed. If the commit is genuinely safe, a maintainer must
integrate it manually or change the manifest in a reviewed PR. Do not reclassify
a protected UI path just to clear a gate.

### Cherry-pick conflict

The sync aborts the cherry-pick, restores the base commit, and reports the
conflicting commit as pending. Unresolved conflict state is never pushed. The
review PR contains a report only.

To integrate manually:

```bash
git fetch origin
git switch -c manual/upstream-<short-sha> origin/main
git cherry-pick -n <sha>
# Resolve each conflict by preserving the custom UI edits.
git diff --cached
git commit
```

Then open a normal PR, run the full build, and verify the UI with
[UI-TESTING.md](UI-TESTING.md). After merging, add `Upstream-SHA: <sha>` to the
review PR body and merge it (or record the integration with the state tools) so
the sync does not retry the commit.

Do not use `git cherry-pick --abort` blindly on a sync-created branch; the sync
already aborted its own attempt.

### Branch push rejected (`force-with-lease` failure)

A concurrent update changed the remote automation branch. Sync, merge recording,
build recording and release recording all share the `automation-state-write`
concurrency group and queue rather than cancel each other, but a manual push or
a branch update from outside the automation can still change the remote SHA.

Rerun the workflow. It reconstructs the candidate from state and the
default-branch tip, then re-pushes with the current remote SHA. Do not use plain
`git push --force` on an automation branch.

### State save fails, or the workflow stops after pushing branches

The publish order is: push automation branches, save state, then update PRs. If
state saving fails, a rerun reconciles everything. The state worktree helper
retries a non-fast-forward push by rebasing on `origin/automation/state`. If it
reports `could not rebase state branch`, a concurrent writer or a manual edit
made the state non-fast-forward.

A state-writing job may sit in `queued` state while another job in the
`automation-state-write` group finishes. That is normal serialization, not a
failure; do not cancel it. Start troubleshooting only when the job runs and the
helper fails.

1. Inspect `git log --oneline origin/automation/state -10`.
2. Prefer a merge/rebase commit from the automation over rewriting history.
3. If manual repair is required, see [ROLLBACK.md](ROLLBACK.md).

### Sync does not update the PR automatically

If `AUTOMATION_TOKEN` is not configured, `GITHUB_TOKEN` pushes do not trigger
`pull_request` workflows. The sync workflow explicitly dispatches
`validate-build.yml` and `ui-protection.yml`. If those dispatches fail, the
token lacks `actions: write`.

If the validation run exists but has no check on the PR head, remember that fork
PRs cannot publish checks with the PR token; automation candidates are
same-repository branches.

### The PR body markers look stale

The candidate PR is updated from the generated `sync-report/candidate-pr.md` on
every sync. Do not edit the hidden markers by hand. If the source of truth and
the PR disagree, rerun the sync and inspect `change-report.json`.

## UI protection

### `ui-protection` is red

Open the check on the head commit. It contains the same Markdown summary as the
`ui-protection-<pr>-<run>` artifact. `blocked` means the PR touches protected UI
without the acknowledgement label.

A maintainer must:

1. Review the actual diff, not only the report.
2. Complete the relevant checklist in [UI-TESTING.md](UI-TESTING.md).
3. Add the `ui-protection:approved` label.

Adding the label triggers the workflow again (the workflow listens for
`labeled`). The check becomes green as `acknowledged`. Acknowledging does not
mean the change is correct; it means the protected change was explicitly
reviewed.

If the report is empty or the job never ran:

- Confirm the PR is open and the head SHA is resolvable.
- Confirm the workflow was not cancelled by concurrency.
- For manual runs, pass a numeric `pr_number` to `ui-protection.yml`.

The classifier runs from the base branch. Editing `.github/ui-protection.yml`
in the PR does not change how the PR is judged; it changes the policy only after
it merges.

### The manifest validation fails

`validate_automation.py` fails when a `patch.sh` section header is missing from
`patch_sections`, or when the manifest declares a section that no longer exists.
Update the manifest in the same PR that changes `patch.sh`. Give every new
section an explicit protection and reason; do not leave it to the default.

`inspect-project.yml` reports the same mismatch and is a convenient manual check.

## Validation and build gate

### `build-gate` is red on a change that does not need a build

The gate check should be green after the lightweight job when the policy says no
build is required. If it is red:

1. Open the check summary. It lists the reasons from `build-policy.json`.
2. Confirm the changed files: `git diff --name-status origin/main...HEAD`.
3. Any `patch.sh` change or any path with `requires_build: true` needs a build.
   Unclassified paths also need a build by default.
4. If the policy is wrong, fix the manifest in a reviewed PR; do not hand-post a
   green check.

### A full build was scheduled but the check stays red

1. Check whether the `full-build` job is waiting for approval on the
   `chromium-build` environment. Approve it only after confirming the PR is
   same-repository and trusted.
2. Check whether the build failed. The `record-build` job posts a failing
   `build-gate` with the build result and state save outcome.
3. Check whether the artifact was recorded: the state branch should contain
   `builds/<tree>.json` for the PR head tree.
4. Check whether the artifact upload/download failed. A successful build with no
   downloaded artifact is a failure, not a green gate.

After fixing an infrastructure failure, rerun the full workflow. A failed or
missing build cannot be converted to green by re-running only `record-build`;
that job records what the build produced.

### The full build was skipped for a fork PR

This is intentional. Fork PRs do not get the write token, cannot post checks
and cannot run on the self-hosted build host. Bring the change into a
same-repository branch or integrate it manually with maintainer review.

### `validate-build` was dispatched but the PR has no check

The workflow posts explicit check runs on the PR head for same-repository PRs.
Check that:

- the dispatch supplied the correct `pr_number` and `mode`;
- the workflow run did not fail before the check-posting step;
- the PR head has not moved since resolution (checks are posted to the resolved
  head SHA).

If the head moved, dispatch again for the current PR.

### Local static validation fails

Run:

```bash
python .github/scripts/validate_automation.py --repo .
python -m unittest discover -s .github/scripts/tests -v
```

Fix all errors. Common causes are an unpinned action, a missing top-level
`permissions` block, `pull_request_target`, an unsafe expression in `run:`, a
missing required workflow, or a `patch.sh`/manifest section mismatch. Warnings
about versions on pinned actions are informational.

## Auto-merge

Auto-merge is blocked unless every condition below is true. `automerge_gate.py`
prints the full problem list; the merge job does not run when the precheck is
false.

| Symptom / problem | Fix |
| --- | --- |
| `UPSTREAM_AUTOMERGE_ENABLED` is not `true` | Set the repository variable only if the team wants this path enabled. |
| `policy.auto_merge` is false | Reviewed manifest change required. |
| PR is not an upstream-sync candidate | Auto-merge is only for the candidate branch marker format. |
| PR has no target marker | Regenerate the candidate PR body; do not hand-edit markers. |
| PR says `automerge: no` | The candidate has pending commits, risk above low, or a required build. |
| Candidate has pending commits | Resolve them separately; do not merge the candidate as if they were included. |
| State candidate target/commits do not match | The PR is stale; rerun the sync and wait for the new head. |
| Candidate risk is not `low` | A human must merge. Chromium/dependency/security changes are not auto-merge material. |
| Candidate requires a full build | A human must merge after an exact-tree build. |
| A required check is missing or not green | Fix the check; do not bypass branch protection. |
| Label actor is not write-or-better | A maintainer must add the label or comment `/automerge`. |
| PR is a draft | Mark it ready for review; the gate refuses drafts. |
| PR does not target the default branch | Rebase onto the default branch; auto-merge never merges into another branch. |
| The `automation-automerge` label was removed while approval was pending | Re-add it and trigger evaluation again; the gate rechecks the label on the fresh PR payload. |
| PR is not mergeable or head changed | Rebase/rerun; the gate checks the exact head SHA. |
| The `upstream-automerge` environment is waiting | A required reviewer must approve the deployment. |

The label alone does not start a run. The gate is evaluated when someone
comments `/automerge`, when a validation workflow completes, or on manual
dispatch. Add the label first, then trigger one of those events.

To disable auto-merge immediately, set `UPSTREAM_AUTOMERGE_ENABLED=false` and/or
remove the label. Also remove the environment's auto-approval if one exists.

## Release

### The plan job rejects the request

- **Ref is not the default-branch tip**: merge the release commit first; release
  only from the current tip of `main`.
- **Version mismatch**: the input must match the four-part version in
  `vanadium/args.gn`; do not invent a release version.
- **Tag/release already exists**: a tag is immutable. If an unpublished draft
  release owns the tag, delete the draft and its tag deliberately; otherwise
  wait for a new upstream version.

### Signing prerequisites are missing

The `release` environment must provide both base64 values. A multiline or
wrapped base64 string fails or produces an invalid keystore. Encode on Linux:

```bash
base64 -w0 keystore.jks > keystore.b64
base64 -w0 local.properties > local.properties.b64
```

Store the file contents as the two environment secrets. Never put release
signing material in repository files or in the test build environment.

### The device smoke test fails

The release workflow intentionally fails when no arm64 device is attached, when
the process does not start, or when the app writes a crash entry. Attach a
dedicated arm64 device to the release host, unlock it, and rerun. Do not bypass
the smoke test to publish; use `publish: false` to inspect the signed artifacts
without publishing.

If the app starts but is not the resumed activity, the workflow warns but does
not fail. Review `smoke.png` and `smoke-report.md` manually before publishing.

### Certificate or package checks fail

- `RELEASE_CERT_SHA256` must be the SHA-256 of the release signing certificate,
  with or without colons; the workflow normalizes it.
- The APK package id must match `chrome_public_manifest_package` in the root
  `args.gn` (`io.github.jqssun.helium` in the current tree).
- The AAB and both APKs must be signed by the same release key.
- A test-signed artifact can never satisfy release verification.

### Publish rejects the durable release marker or an artifact hash

`publish` requires `builds/<tree>.json` on `automation/state` for the release
tree, with `kind: release`, `signing: release`, the same commit, repository and
build run id, and a SHA-256 and size for every downloaded asset. It also requires
the state marker and `release-manifest.json`/`SHA256SUMS` to agree.

1. Confirm the run's `record` job succeeded and wrote the marker for the exact
   tree of the release commit (`git rev-parse <sha>^{tree}`).
2. Do not edit the marker by hand to clear the failure. Rerun `release.yml` so
   the build and record steps produce a consistent marker and artifact set.
3. If `SHA256SUMS`, `release-manifest.json` or `smoke-report.md` is empty, the
   upload/download step failed; inspect the build and smoke-test logs.
4. An extra downloaded file, a different run id or a test-build marker is
   rejected by design; it indicates a poisoned or stale artifact set.

### Publish fails after a successful build

`publish-release` re-checks that the release commit is still the tip of `main`
and that the tag/release does not exist. If `main` moved, rerun the release from
the new tip. If the tag was created by a concurrent process, do not force it;
resolve the tag ownership first. The workflow is safe to rerun when the
published release does not exist.

### A test build appears to have overwritten the release marker

It cannot: `build_record.py` refuses to replace an existing successful
`kind: release` marker for the same tree with a `kind: test` marker. If the state
branch appears otherwise, inspect the record job logs and the marker history;
do not resolve it by deleting the release marker. Rebuild and record the release
through `release.yml`.

## Useful diagnostics

```bash
# Inspect a state file without checking out the state branch
git fetch origin automation/state
git show origin/automation/state:state.json

# Find failed-build markers for a tree
git ls-tree -r --name-only origin/automation/state | grep '^builds/failures/'

# Validate a commit range locally
python .github/scripts/ui_guard.py --repo . --base origin/main --head HEAD
python .github/scripts/build_policy.py --repo . --base origin/main --head HEAD

# Inspect the last sync report
gh run download <run-id> -n upstream-sync-<run-id>
```

When a workflow behaves differently from this document, trust the workflow and
script source in the repository, then update this document. Do not rely on
memory of an older version.
