# Upstream automation

This directory documents the upstream-sync, UI-protection, build-validation and
release automation for Titanium Browser for Android. The automation is designed
for a repository that tracks `jqssun/android-titanium-browser` while carrying
its own UI and branding changes.

The central rule is simple: **safe changes can be proposed automatically;
protected UI and release decisions cannot.** Automation never force-updates
`main`, never pushes an unresolved merge conflict, and never publishes an APK
without a successful signed build and a device smoke test.

## What the automation does

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `inspect-project.yml` | manual, pushes to automation files, weekly | Validate the manifest against the real `patch.sh` sections; report toolchain, disk, RAM, version and submodule state. |
| `upstream-sync.yml` | daily schedule, manual | Fetch upstream, classify commits, rebuild the candidate branch, save durable state, open/update candidate and review PRs, queue validation. |
| `ui-protection.yml` | every pull request, manual | Classify the PR diff against the base-branch manifest and publish the `ui-protection` check on the head commit. |
| `validate-build.yml` | every pull request, manual | Run static validation and build policy on every PR; run an exact-tree full Chromium build when required and approved. |
| `upstream-review.yml` | PR close, `/automerge`, validation completion, manual | Record merged upstream SHAs durably and run the optional, fully gated auto-merge path. |
| `release.yml` | manual only | Build, sign, verify, smoke-test and optionally publish a release from the current default-branch tip. |

The workflows are in `.github/workflows/`; the Python and shell helpers are in
`.github/scripts/`.

## How this repository's UI is protected

Flash Browser is a Chromium fork rebranded from Titanium. Its UI is not a
normal `app/src/main/res` tree:

- `patch.sh` applies sed-based UI, feature and branding edits to the Chromium
  checkout. The script is divided into `# brand`, `# accessibility`, `# dev`,
  `# ext` and `# desktop` sections.
- `res/` contains the launcher/themed icons and icon generation input.
- `fastlane/` contains Play Store listing text, screenshots and store copy.
- `extensions/` contains bundled-extension packaging.
- `args.gn` contains the package id (`io.github.jqssun.helium`) and feature
  configuration.
- `build.sh` and `common.sh` implement checkout, patching, signing and
  packaging.

The policy file is `.github/ui-protection.yml`. It is checked by the trusted
base-branch workflow, so a pull request cannot weaken the classifier that judges
it.

### Protection levels

| Level | Meaning |
| --- | --- |
| `protected` | Custom UI, branding or product identity. The commit is **excluded** from the candidate branch and listed for human review. |
| `review` | Build, CI, legal, dependency or unclassified change. Excluded from the candidate branch; human review required. |
| `candidate` | Safe to propose on the candidate branch. The candidate PR still requires review. |
| `section` | Only for `patch.sh`: each hunk inherits the rule of its section header. |

Patch hunks are classified by the section header nearest above the changed
line. Changes before the first header are unlabeled and default to `review`.
A commit is candidate-eligible only when **every** changed path and every
changed `patch.sh` hunk is candidate-eligible. One protected hunk moves the
whole commit to the review path; the automation never extracts a partial commit.

The default path rule is `review`: a new path is never assumed safe just because
nobody classified it yet.

Both `ui_guard.py` and `build_policy.py` diff the **merge base** of the base and
head commits, not the current tip of `main`. A pull request that is behind the
default branch is therefore judged only for its own changes; protected changes
already on `main` are not attributed to it.

## Sync flow

1. Check out the repository default branch (`main`).
2. Prepare the orphan state branch in a temporary worktree.
3. Fetch the upstream branch (or a manual target) directly from
   `upstream.default_repo` in `.github/ui-protection.yml`.
4. Determine the commit range from `last_scanned_sha` to the target. If
   upstream rewrote history, rescan from the merge base and warn.
5. Classify each unprocessed commit by changed paths and `patch.sh` sections.
   Merge commits and root commits are never cherry-picked automatically.
6. Rebuild `automation/upstream-candidate` from the current default-branch tip:
   carry commits from the previous open candidate, add newly eligible commits,
   and cherry-pick deterministically. A conflict aborts the cherry-pick cleanly
   and turns that commit into a pending-review item. If no candidate commit
   remains publishable, the stored `candidate` is cleared so stale SHAs are not
   carried forever.
7. Push the candidate branch only if it has commits. Push the review branch only
   if a review report exists.
8. Save state, then open/update the PRs.
9. Queue validation if `GITHUB_TOKEN` pushes will not trigger workflows.

The candidate branch is force-pushed with `--force-with-lease`, never rewritten
in place without checking the remote SHA. It is an automation-owned branch and
is not a protected branch.

### Candidate PR

- Base: default branch. Head: `automation/upstream-candidate`.
- Labels: `automation`, `upstream-sync`, plus `full-build` when required.
- Body: a change report plus hidden markers used by the merge recorder and the
  auto-merge gate.
- A merge can record `Upstream-SHA: <sha>` lines explicitly; otherwise every SHA
  in the candidate marker is treated as integrated when the PR merges.

### Review PR

- Head: `automation/upstream-review-<target-prefix>`, unique per upstream target.
- Contains **only a Markdown report**. It never contains conflicted code or a
  partial cherry-pick.
- A merged review PR acknowledges the pending changes. Pending SHAs are moved to
  `deferred` unless the PR body lists them as `Upstream-SHA:` (integrated).
- A review PR does not integrate the upstream code. Integration is a manual
  branch and a normal code review.

## Durable state

The state lives on an orphan branch, `automation/state`, which is never merged
into `main` and contains no product source. It survives runner cleanup and has
its own history.

```
state.json
builds/<tree>.json              successful exact-tree build marker
builds/<tree>.md                human-readable marker
builds/failures/<tree>-<run>-<attempt>.json
merges/pr-<number>.json         merge record
```

`state.json` contains:

- `last_scanned_sha`, `processed_shas`, `integrated_shas`
- `pending[]` and `deferred[]` entries with SHA, subject, reason and risk
- `candidate` (`target_sha`, `shas`, `risk`, `requires_full_build`) or `null`
- bounded `history[]`

Sync reports and discovery reports are workflow artifacts, not state. Artifact
retention is 14-30 days depending on the workflow; durable decisions and build
markers are on the state branch.

`automation/state` is written by `state_branch.sh`, which retries pushes and
rebases on concurrent updates. If it cannot rebase, it exits with an error
instead of discarding a concurrent record. Every job that writes state (sync,
merge recording, build recording and release recording) shares the
`automation-state-write` concurrency group, so two recorders cannot prepare an
update from the same state snapshot and overwrite each other.

A successful release marker (`kind: release`) is never downgraded by a later
test build of the same tree; the test recorder refuses to replace it.

## Build gating

Every same-repository PR gets:

- `lightweight-validation`: static validation of workflows/scripts, manifest
  validation against `patch.sh`, and build-policy evaluation.
- `ui-protection`: the UI classifier result for the PR head.
- `build-gate`: green only when a full build is not required, or when an
  exact-tree full build has succeeded and been recorded.

The required full-build decision is driven by the manifest:
`requires_build: true` on any changed path, or any change to `patch.sh`, makes a
full build mandatory. The classifier defaults to requiring a build for
unclassified paths.

Build success is keyed by the Git **tree** SHA, not a branch name or a commit
subject. The `record-build` job computes `git rev-parse <head>^{tree}`, records
the artifact hashes on the state branch, then posts `build-gate` for the PR
head. A build of a different tree, a failed build, or an unrecorded build leaves
the gate red. Test builds use an ephemeral keystore created on the runner and
are never release artifacts.

Full builds run only for same-repository PRs on the self-hosted runner selected
by `build.runner_labels`. The job is gated by the `chromium-build`
environment and runs with `contents: read`. Fork PRs are excluded and cannot
obtain the write token needed to post checks.

## Auto-merge (off by default)

`policy.auto_merge` is `false` in the manifest, and the candidate PR normally
carries `automerge: no`. Enabling auto-merge requires **all** of the following:

1. A reviewed manifest change setting `policy.auto_merge: true`.
2. Repository variable `UPSTREAM_AUTOMERGE_ENABLED=true`.
3. The `upstream-automerge` environment with required reviewers.
4. A maintainer adds the `automation-automerge` label to the candidate PR. The
   label is a required control, but it does not itself start a run: evaluation
   happens when someone comments `/automerge`, when Validate build completes,
   or on manual dispatch.
5. The PR is open, unmerged, not a draft, mergeable, same-repository, targets the
   repository default branch, and still carries the exact head SHA and the
   `automation-automerge` label.
6. The state branch candidate target and included SHAs match the PR markers.
7. Risk is `low`, no full build is required, and there are no pending commits.
8. `ui-protection`, `lightweight-validation` and `build-gate` are all successful
   on the current head.
9. The label actor has `write`, `maintain` or `admin` permission.

The merge is squash-only with `--match-head-commit`. If any gate fails, no merge
occurs. Chromium revision updates, security-keyword changes, mixed UI changes
and anything requiring a build remain human-merged.

## Release flow

`release.yml` is manual and defaults to **build only** (`publish: false`).

1. `plan` verifies the requested ref is exactly the default-branch tip, reads the
   four-part version from `vanadium/args.gn`, and refuses an existing tag or
   release.
2. `build` runs on the self-hosted `chromium-build` runner behind the `release`
   environment. It requires `RELEASE_KEYSTORE_B64` and
   `RELEASE_LOCAL_PROPERTIES_B64`. `build.sh` receives them in the variable names
   it already expects.
3. Verification checks exactly one arm64 APK, one armv7 APK and one AAB; version
   and package id; APK signatures and certificate consistency; optional
   certificate pin `RELEASE_CERT_SHA256`; AAB signature; and SHA-256 sums.
4. A mandatory device smoke test installs the arm64 APK, launches it, checks the
   process and resumed activity, captures a screenshot and fails on a crash
   buffer entry for the package. No attached arm64 device means no release.
5. `record` writes a release build marker to the state branch.
6. `publish` runs only when `publish: true`, behind `release-publish`. It
   re-checks that the commit is still the default-branch tip and the tag/release
   is still absent, verifies the downloaded checksums, confirms the manifest and
   smoke-test result, and then requires the durable `builds/<tree>.json` release
   marker to match the downloaded artifacts, commit, repository and run before
   attesting provenance and creating a GitHub release (draft by default).
   `verify_release_artifacts.py` checks the artifact set, sizes and SHA-256
   values; an artifact that is missing, extra, truncated or altered fails the
   publish job.

## Setup

### 1. Point the automation at the right upstream

For a fork, keep `.github/ui-protection.yml` pointing at the upstream project:

```yaml
upstream:
  default_repo: "jqssun/android-titanium-browser"
  default_branch: "main"
```

Do not point it at the fork itself unless you intentionally want to sync the
fork's own commits. The automation checks out the fork's default branch as the
integration base.

Customizations must be reflected in the manifest. If you add a new custom UI
directory or file, add a `protected` or `review` path rule **before** merging the
customization. Unclassified paths are reviewed, not auto-integrated, but an
explicit rule gives a better reason and the correct build requirement.

### 2. Runner

A full Chromium build needs a dedicated Linux x64 host. The default labels are:

```yaml
self-hosted
linux
x64
chromium-build
```

The preflight expects at least 100 GiB free disk (configured as
`build.min_free_disk_gib`) and recommends 32 GiB RAM. Required tools: `git`,
`python3`, `bash`, `java`; the build itself needs the Chromium toolchain and
Android SDK dependencies that `build.sh` normally provisions. Do not run
untrusted fork code on this host; only same-repository PRs reach the full-build
job.

### 3. Environments

Create these GitHub environments under **Settings > Environments**:

| Environment | Used by | Recommended protection |
| --- | --- | --- |
| `chromium-build` | full test builds | Required reviewers; restrict to the build host; no release secrets. |
| `release` | signed release build | Required reviewers; release signing secrets only. |
| `release-publish` | publishing | Required reviewers; limit to the default branch. |
| `upstream-automerge` | optional auto-merge | Required reviewers; only if auto-merge is enabled. |

Environment reviewers are the last human gate for builds and releases. The
workflow files cannot replace environment protection rules.

### 4. Secrets and variables

Secrets (repository or environment scope as appropriate):

| Name | Required | Used for |
| --- | --- | --- |
| `AUTOMATION_TOKEN` | optional | Pushes and PR creation that trigger normal PR workflows. Without it, sync dispatches validation explicitly. |
| `RELEASE_KEYSTORE_B64` | release only | Base64 of the release keystore; mapped to `STORE_TEST_JKS`. |
| `RELEASE_LOCAL_PROPERTIES_B64` | release only | Base64 of `local.properties` (`keyAlias`, `keyPassword`, `storePassword`); mapped to `LOCAL_TEST_JKS`. |

`RELEASE_*` secrets belong to the `release` environment, not to the repository
globally, so they are not exposed to test builds or PR jobs. The `chromium-build`
environment deliberately has no release secrets.

Variables:

| Name | Required | Used for |
| --- | --- | --- |
| `UPSTREAM_AUTOMERGE_ENABLED` | only for auto-merge | Must be exactly `true`. |
| `RELEASE_CERT_SHA256` | strongly recommended | Pins the expected APK signing certificate digest; a mismatch fails the release. |

For `AUTOMATION_TOKEN`, use a fine-grained token limited to this repository with
`contents: write`, `pull requests: write`, `issues: write` and `actions: write`
(the sync workflow uses `actions: write` to dispatch validation). A classic PAT
needs the equivalent repo/actions scopes. If the token is absent, automation
still works, but `GITHUB_TOKEN` pushes will not emit `pull_request` events; the
workflow explicitly dispatches `validate-build.yml` and `ui-protection.yml`
instead.

### 5. Labels

The sync workflow creates these labels if missing:

- `automation`
- `upstream-sync`
- `upstream-review`
- `needs-human-review`
- `full-build`
- `automation-automerge`

Create `ui-protection:approved` manually. A maintainer applies it only after
reviewing protected UI changes. It acknowledges the protected diff; it does not
replace the required checks or a full build.

### 6. Branch protection

On `main`, require the status checks named exactly:

- `ui-protection`
- `lightweight-validation`
- `build-gate`

Also require a pull request review, disallow force pushes and deletions, and
consider CODEOWNERS for `.github/**`, `patch.sh`, `res/**`, `fastlane/**`,
`extensions/**`, `args.gn`, `build.sh` and `common.sh`.

The workflow jobs also have display names (`ui-protection-workflow`,
`lightweight-validation-workflow`, `full-build`, `record-build`). The explicit
check names above are the ones to require; they are posted on the head commit by
`post_check.py`, including for dispatched runs.

### 7. Optional auto-merge and releases

Only enable `upstream-automerge` after all checks and branch protection are
working and the team is comfortable with the gate. Auto-merge is intended for
small, low-risk, candidate-only changes; it is not a general merge button.

Before the first release, run `release.yml` with `publish: false`, confirm the
signed artifacts and smoke report, then run again with `publish: true` from the
same default-branch tip. The release job refuses a moving target.

## Security model

- No `pull_request_target`. PR workflows use `pull_request` with read-only
  top-level permissions and do not receive repository secrets.
- Third-party actions are pinned to full commit SHAs; unpinned actions fail
  static validation.
- The UI classifier and manifest are read from the base branch for PR checks.
- Test build jobs have `contents: read` and only receive an ephemeral keystore.
  The separate recording job has write access but executes trusted scripts from
  the base branch.
- Fork PRs are excluded from full builds, explicit check publishing and
  auto-merge.
- Release signing material exists only in the `release` environment. Publishing
  is a separate environment and re-verifies the default-branch tip, tag absence,
  checksums, the durable release marker and the exact artifact hashes before
  creating assets.
- Release artifacts are attested; test artifacts are never published, and a test
  build cannot replace a recorded release marker for the same tree.
- The state branch is append-only in normal operation and is never merged into
  `main`.

Residual risks that require human process:

- A same-repository PR can modify workflow files. Workflow changes are
  `review`-level in the manifest, but branch protection, CODEOWNERS and human
  review must enforce them. GitHub executes the workflow definition from the PR
  head once a reviewer approves the run; the `chromium-build` environment gate
  is the last line of defense.
- The self-hosted build host executes PR code. It must be dedicated, ephemeral
  where possible, and must not hold release secrets or other credentials.
- A maintainer with repository write access can apply the acknowledgement label.
  The label is an acknowledgement, not a substitute for review.
- GitHub's check runs on fork PRs cannot be written by the PR token; the branch
  protection policy for fork contributions should require maintainer review.

## Local validation

On a GitHub Ubuntu runner use `python3`. On Windows with the local toolchain use
`python` (the `python3` alias may open the Microsoft Store):

```bash
python .github/scripts/validate_automation.py
python -m unittest discover -s .github/scripts/tests -v
python .github/scripts/discover.py --repo . --output-dir discovery-report
python .github/scripts/ui_guard.py --repo . --base origin/main --head HEAD
python .github/scripts/build_policy.py --repo . --base origin/main --head HEAD
```

`validate_automation.py` compiles every Python file, runs `bash -n` on shell
files, checks action pinning and workflow security rules, and verifies every
`patch.sh` section is declared in the manifest. The unit suite exercises the
classifier, patch parsing, state normalization, the sync/record/build-policy
flow against temporary Git repositories, the auto-merge and release-verification
helpers. Neither proves that a workflow or Chromium build succeeds on GitHub.

## Implementation status

Implemented in this repository and statically validated locally:

- Six workflows, the protection manifest, the classifier, sync/state/build/
  release/merge scripts, the change-report schema and this documentation.
- Local static validation passes with 0 errors and 0 warnings.
- The local test suite passes (53 tests at the last run) against temporary Git
  repositories; it includes conflict-abort, deterministic rerun, stale-candidate
  clearing, merged-SHA recording, no-common-ancestor refusal, merge-base diffing,
  PR marker round-trips, release marker protection and artifact tampering.
- The manifest matches all 23 `patch.sh` sections in the checked-out source.

Not executed in this environment:

- No workflow has run on GitHub; no branch, PR, state branch, artifact or
  release has been created by this automation yet.
- No full Chromium build, release build or device smoke test has been run here.
- The self-hosted runner, environments, secrets, variables, labels and branch
  protection must be configured in GitHub by a maintainer.
- Fork-specific customization rules must be reviewed by the fork owner; this
  checkout is the upstream repository and does not contain a separate custom UI
  tree beyond what `patch.sh`, `res/`, `fastlane/` and `extensions/` already
  define.
