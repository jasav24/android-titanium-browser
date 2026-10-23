# Rollback and recovery

This guide covers stopping the automation, undoing an integrated change,
repairing the state branch, and handling a bad release. Read the whole section
before running anything destructive.

General rules:

- **Never rewrite `main`.** Revert with a normal PR; do not force-push the
  default branch.
- **Never delete the state branch to make a problem disappear.** It contains
  the only durable record of what was integrated. Export it first.
- **Do not force-push an automation branch except through
  `--force-with-lease` with the current remote SHA**, and only when the steps
  below call for it. Plain `--force` is rejected by automation validation.
- Preserve evidence (run logs, `state.json`, merge records, release assets)
  before changing anything.
- A published release or Play Store rollout cannot be made private by deleting
  a GitHub release. See [Release rollback](#release-rollback).

## 1. Stop the automation

Disable the scheduled sync and the auto-merge workflow:

```bash
gh workflow disable "Upstream sync"
gh workflow disable "Upstream review and auto-merge"
```

Cancel an in-progress run if necessary:

```bash
gh run list --workflow "Upstream sync" --status in_progress
gh run cancel <run-id>
```

Disable auto-merge independently of the workflows:

```bash
gh variable set UPSTREAM_AUTOMERGE_ENABLED --body false
```

Re-enable later with `gh workflow enable "Upstream sync"` and
`gh workflow enable "Upstream review and auto-merge"`. Do not re-enable the
scheduled workflow until the state has been repaired and a manual dry run
looks correct.

## 2. Revert an integrated candidate or review PR

A merged candidate PR is an ordinary squash commit on `main`. Revert it with a
normal revert PR:

```bash
git fetch origin
git switch -c revert/upstream-sync-<pr-number> origin/main
git revert --no-edit <squash-merge-sha>
git push -u origin HEAD
gh pr create --base main --head revert/upstream-sync-<pr-number> \
  --title "revert: upstream sync PR #<pr-number>" \
  --body "Reverts the upstream integration merged in #<pr-number>."
```

Use the PR's merge commit SHA (`gh pr view <n> --json mergeCommit`). For a
squash merge this is a single-parent commit; `git revert` is correct. For a
merge commit, use `git revert -m 1 <merge-sha>` and inspect the result.

After the revert merges:

- The revert creates a new tree. Old build markers remain valid only for the
  old tree; the new tree needs its own build before it can carry a green gate.
- The upstream SHA is still in `integrated_shas`. The sync will not retry it.
  If you intentionally want to retry that upstream commit later, remove its SHA
  from `integrated_shas` and `processed_shas` in `state.json` (and from
  `pending`/`candidate` if present), commit the state change, then run the sync
  with `retry_pending: true`. Do this only after understanding why it was
  reverted.

To make the sync pick up a previously rejected commit again, add it to
`--retry-pending` by running the workflow with `retry_pending: true`. For a
commit that was integrated and then reverted, the state edit above is required
first.

## 3. Abandon the current candidate

Close the candidate PR first, then clear the candidate in state. If you only
close the PR, the next sync still sees `state.candidate` and will carry those
commits into a new candidate branch and PR.

```bash
gh pr close <pr-number> --comment "Candidate abandoned; state candidate will be reset."
```

Prepare the state worktree:

```bash
git fetch origin
git worktree add --detach /tmp/titanium-automation-state origin/automation/state
cd /tmp/titanium-automation-state
```

Edit `state.json`:

- Set `"candidate": null`.
- If the commits should never be retried, leave them in `processed_shas`.
- If they should be reconsidered later, remove them from `processed_shas` and
  make sure they are not in `integrated_shas`.

Commit and push normally:

```bash
git add state.json
git commit -m "state: abandon candidate"
git push origin HEAD:automation/state
```

Delete the automation-owned candidate branch only after the PR is closed and the
state is updated:

```bash
git push origin --delete automation/upstream-candidate
```

Do not delete a branch that contains human commits. The candidate branch should
contain only automation cherry-picks.

## 4. Abandon or reset a review item

A review PR is report-only. Closing it does not integrate anything. Pending
SHAs may remain in `state.json` until a review PR is merged or the state is
edited.

To stop retrying a pending commit, remove its entry from `pending` and
`deferred`. To retry it after a manifest fix, run the sync with
`retry_pending: true`; pending and deferred SHAs are added to the work pool.

If a review PR was merged after code was integrated manually, ensure the PR body
contains `Upstream-SHA: <sha>` for every integrated commit **before** merging.
Otherwise the merge recorder moves the commits to `deferred` and a later sync
may try to cherry-pick them again.

To correct a mistaken merge record, edit `state.json`:

- Add the SHA to `integrated_shas` if the code is present.
- Remove it from `pending` and `deferred`.
- Do not remove the corresponding `merges/pr-<number>.json`; it is an audit
  record. If the record is wrong, write a corrective state commit and explain
  it in the commit message.

## 5. Repair the state branch

### A bad state commit

Prefer reverting the commit instead of rewriting history:

```bash
git fetch origin
git worktree add --detach /tmp/titanium-automation-state origin/automation/state
cd /tmp/titanium-automation-state
git log --oneline -10
git revert --no-edit <bad-state-commit>
git push origin HEAD:automation/state
```

### Roll back to an earlier state

This loses any records written after the chosen commit. Export the current
branch first:

```bash
git fetch origin
git worktree add --detach /tmp/titanium-automation-state origin/automation/state
cd /tmp/titanium-automation-state
git switch -C automation/state <good-commit>
git push --force-with-lease=refs/heads/automation/state:$(git rev-parse origin/automation/state) \
  origin HEAD:refs/heads/automation/state
```

If the lease fails, someone else updated the branch. Fetch again, compare, and
decide whether the concurrent record must be preserved. Do not retry with plain
`--force`.

### State push conflicts (`exit 66`)

`state_branch.sh` fetches and rebases on `origin/automation/state` when a push
fails. If it cannot rebase, stop and inspect:

```bash
git fetch origin
git log --oneline origin/automation/state -10
```

Resolve on a temporary worktree, then push a normal commit. Never discard a
concurrent merge record merely to make the push succeed.

### Rebuild state.json from scratch

This is a last resort. Export the old branch, then construct a minimal state:

```json
{
  "schema_version": 1,
  "upstream": "jqssun/android-titanium-browser",
  "created_at": "<ISO-8601>",
  "updated_at": "<ISO-8601>",
  "last_scanned_sha": null,
  "processed_shas": [],
  "integrated_shas": [],
  "pending": [],
  "deferred": [],
  "candidate": null,
  "history": []
}
```

An empty state causes the sync to rescan from the merge base and may reconsider
commits that were already integrated. Prefer restoring a known-good state
commit and replaying only the missing records.

### Build markers

`builds/<tree>.json` is a claim that this exact tree was built successfully with
the recorded artifacts. Do not create a marker by hand and do not copy a marker
from a different tree. If a marker is invalid and a build must be re-run, delete
that tree's marker in a normal state commit and rerun the build. A missing
marker makes the gate red; that is the safe failure mode.

## 6. Recover a deleted or broken workflow file

All automation is versioned. Restore from `main` or from the commit before the
bad change:

```bash
git fetch origin
git switch -c fix/automation-workflows origin/main
git checkout <good-commit> -- .github/workflows .github/scripts .github/ui-protection.yml
git diff --cached
git commit -m "fix(automation): restore known-good workflow and scripts"
git push -u origin HEAD
```

Open a PR. Do not push workflow changes directly to `main`; automation file
changes are themselves review-level, and the trusted base copies are what run
the gate.

## 7. Release rollback

### The build failed, or publish was refused

No release was created. Fix the cause and rerun `release.yml` from the same
default-branch tip. With `publish: false` the signed artifacts are retained as
workflow artifacts for inspection.

### A draft release is wrong

A draft is not public. Delete it and its tag deliberately:

```bash
gh release view <tag>
gh release delete <tag> --yes --cleanup-tag
```

Deleting a draft tag is acceptable when the draft has not been published. If the
tag was pushed independently of the draft, confirm ownership before deleting it.

### A published release is wrong

Do not silently delete a published release: users may already have downloaded
it, and the attestation may be referenced elsewhere. Instead:

1. Stop further publication (`gh workflow disable "Release"`).
2. Mark the release as a draft to remove it from the public releases page:
   `gh release edit <tag> --draft`.
3. If a replacement is needed, fix the code and release a new version; do not
   reuse or move the tag.
4. Write an incident note that states which artifacts were affected and which
   signing key was used.
5. If signing material was exposed, rotate the signing key and treat previously
   signed artifacts as compromised. Do not attempt to re-sign the same tag with a
   different key as if nothing happened.

Deleting the release entirely is a policy decision for the maintainers, not an
automation step:

```bash
# Only after the incident decision, and only with maintainer approval:
gh release delete <tag> --yes --cleanup-tag
```

A Google Play rollout is outside this repository. Use the Play Console to halt
or replace a rollout; deleting a GitHub release does not affect Play.

### A published APK is found to be bad

1. Disable the Release workflow.
2. Mark the GitHub release as a draft or add a prominent warning to its notes.
3. Halt the Play rollout if applicable.
4. Preserve the artifacts and the release manifest for investigation; do not
   overwrite the tag.
5. Fix forward with a new version. Do not publish an untested rebuild under the
   same version.

## 8. Emergency checklist

For a suspected bad integration or release, in order:

1. Disable `Upstream sync`, `Upstream review and auto-merge`, and `Release`.
2. Set `UPSTREAM_AUTOMERGE_ENABLED=false`.
3. Export the state branch and the failing run logs.
4. Identify the affected merge/release and preserve its records.
5. Revert with a normal PR, or withdraw the release as described above.
6. Repair state only after deciding whether commits must be retried.
7. Re-enable automation one workflow at a time and run a manual dry run before
   restoring the schedule.
