#!/usr/bin/env python3
"""Create a safe upstream-sync candidate branch and a review branch.

This script never pushes and never updates the stable branch. The workflow
pushes the branches it creates and persists the JSON state on the state
branch. Cherry-picks are attempted atomically per upstream commit: a conflicted
commit is aborted and reported instead of leaving conflict state behind.

The candidate branch is rebuilt deterministically from the base commit plus the
currently open candidate SHAs, so rerunning the workflow is idempotent. The
committer date is inherited from the upstream commit to keep the resulting
SHAs stable when neither the base nor the upstream range changed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from automation_lib import (
    CANDIDATE_BRANCH,
    REVIEW_BRANCH,
    REVIEW_DIR,
    AutomationError,
    candidate_pr_body,
    classify_commit,
    commit_metadata,
    emit_github_output,
    ensure_clean_worktree,
    git,
    git_out,
    is_ancestor,
    load_manifest,
    load_state,
    now_utc,
    overall_risk,
    report_markdown,
    review_file_markdown,
    review_pr_body,
    rev_parse,
    save_state,
    sha_list,
    short_sha,
    write_json,
    write_text,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository worktree")
    parser.add_argument("--upstream", required=True, help="upstream Git URL or path")
    parser.add_argument("--upstream-ref", default="refs/heads/main")
    parser.add_argument("--base", default="HEAD", help="candidate base ref")
    parser.add_argument("--target", help="skip fetching and use this upstream commit")
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--candidate-branch", default=CANDIDATE_BRANCH)
    parser.add_argument("--review-branch", default=REVIEW_BRANCH)
    parser.add_argument("--retry-pending", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument(
        "--upstream-name",
        help="human-readable upstream name for state/reports (defaults to --upstream)",
    )
    return parser.parse_args(argv)


def rev_list(base: str, target: str, cwd: Path) -> list[str]:
    output = git(
        "rev-list", "--reverse", "--topo-order", f"{base}..{target}", cwd=cwd
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def commit_exists(sha: str, cwd: Path) -> bool:
    return (
        git("cat-file", "-e", f"{sha}^{{commit}}", cwd=cwd, check=False).returncode
        == 0
    )


def commit_time(sha: str, cwd: Path) -> str:
    return git_out("show", "-s", "--format=%ct", sha, cwd=cwd)


def pending_entry(commit: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "sha": commit["sha"],
        "subject": commit.get("subject", ""),
        "reason": reason,
        "risk": commit.get("risk", "high"),
        "first_seen": now_utc(),
        "protection": commit.get("protection", "review"),
        "requires_build": bool(commit.get("requires_build")),
    }


def update_pending(
    state: dict[str, Any],
    retried: set[str],
    new_pending: list[dict[str, Any]],
) -> None:
    existing = {
        entry["sha"]: entry
        for entry in state.get("pending", [])
        if entry.get("sha")
    }
    kept = [
        entry for sha, entry in existing.items() if sha not in retried
    ]
    by_sha = {entry["sha"]: entry for entry in kept}
    for entry in new_pending:
        previous = existing.get(entry["sha"])
        if previous and previous.get("first_seen"):
            entry["first_seen"] = previous["first_seen"]
        if previous and previous.get("report"):
            entry["report"] = previous["report"]
        by_sha[entry["sha"]] = entry
    state["pending"] = list(by_sha.values())


def update_deferred(state: dict[str, Any], retried: set[str]) -> None:
    state["deferred"] = [
        entry
        for entry in state.get("deferred", [])
        if entry.get("sha") not in retried
    ]


def cherry_pick(sha: str, repo: Path) -> tuple[str, str | None]:
    """Return ("picked"|"already_present"|"conflict", detail)."""
    meta = commit_metadata(sha, repo)
    env = {
        "GIT_COMMITTER_DATE": meta.get("committer_date") or meta.get("date") or "",
    }
    before = rev_parse("HEAD", cwd=repo)
    result = git(
        "cherry-pick", "-x", "--empty=drop", sha, cwd=repo, check=False, env=env
    )
    if result.returncode == 0:
        after = rev_parse("HEAD", cwd=repo)
        if after == before:
            return "already_present", "Cherry-pick produced an empty commit."
        return "picked", None

    conflicted_files = git(
        "diff", "--name-only", "--diff-filter=U", cwd=repo, check=False
    ).stdout.splitlines()
    abort = git("cherry-pick", "--abort", cwd=repo, check=False)
    if abort.returncode != 0:
        git("cherry-pick", "--quit", cwd=repo, check=False)
        raise AutomationError(
            f"cherry-pick of {sha} failed and could not be aborted cleanly; "
            "refusing to continue and leave conflict state behind.\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    reason = "Cherry-pick conflict"
    if conflicted_files:
        reason += " in: " + ", ".join(sorted(set(conflicted_files))[:10])
    return "conflict", reason


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    manifest = load_manifest(repo / args.manifest)
    policy = manifest["policy"]
    upstream_name = args.upstream_name or args.upstream

    ensure_clean_worktree(repo)
    base_sha = rev_parse(args.base, cwd=repo)

    if args.target:
        target_sha = rev_parse(args.target, cwd=repo)
    else:
        if not args.no_fetch:
            env = dict(os.environ)
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            git(
                "fetch",
                "--no-tags",
                "--quiet",
                args.upstream,
                args.upstream_ref,
                cwd=repo,
                check=True,
                env=env,
            )
        target_sha = rev_parse("FETCH_HEAD", cwd=repo)

    state = load_state(args.state_file, upstream_name)
    state_file_existed = Path(args.state_file).exists()
    last_scanned = state.get("last_scanned_sha")
    processed = set(state.get("processed_shas", []))
    integrated = set(state.get("integrated_shas", []))
    previous_candidate = state.get("candidate") or {}
    carried = [
        sha
        for sha in previous_candidate.get("shas", [])
        if sha not in integrated
    ]

    retry_set: set[str] = set(carried)
    if args.retry_pending:
        retry_set.update(
            entry["sha"] for entry in state.get("pending", []) if entry.get("sha")
        )
        retry_set.update(
            entry["sha"] for entry in state.get("deferred", []) if entry.get("sha")
        )

    warnings: list[str] = []
    if last_scanned and is_ancestor(last_scanned, target_sha, cwd=repo):
        range_base = last_scanned
    else:
        merge_base = git(
            "merge-base", base_sha, target_sha, cwd=repo, check=False
        ).stdout.strip()
        if not merge_base:
            raise AutomationError(
                "base and upstream have no common ancestor; refusing to sync"
            )
        if last_scanned:
            warnings.append(
                "Upstream history was rewritten (last scanned SHA is no longer an "
                f"ancestor); rescanning from merge-base {short_sha(merge_base)}."
            )
        range_base = merge_base

    reachable = rev_list(range_base, target_sha, cwd=repo)
    reachable_set = set(reachable)
    new_commits = [sha for sha in reachable if sha not in processed]

    if not new_commits and not retry_set:
        report = base_report(
            args,
            manifest,
            base_sha,
            target_sha,
            last_scanned,
            state,
            result="no_new_commits",
        )
        report["next_steps"] = ["No unprocessed upstream commits. Nothing to do."]
        if not state_file_existed and not args.dry_run:
            state["last_scanned_sha"] = target_sha
            finish(report, args, policy, state, state_changed=True)
        else:
            finish(report, args, policy, state, state_changed=False)
        return 0

    missing_retry = sorted(
        sha
        for sha in retry_set
        if sha not in reachable_set and not commit_exists(sha, repo)
    )
    if missing_retry:
        warnings.append(
            "Some retried SHAs no longer exist in the object database: "
            + ", ".join(short_sha(sha) for sha in missing_retry)
        )

    carry_set = {sha for sha in carried if sha not in missing_retry}
    carry_pool = [sha for sha in carried if sha in carry_set]
    extra_retry = sorted(
        (
            sha
            for sha in retry_set
            if sha not in reachable_set
            and sha not in carry_set
            and commit_exists(sha, repo)
        ),
        key=lambda sha: commit_time(sha, repo),
    )

    work_pool = [
        sha
        for sha in reachable
        if sha in (set(new_commits) | retry_set)
        and sha not in carry_set
        and sha not in integrated
    ]
    work_pool.extend(
        sha
        for sha in extra_retry
        if sha not in carry_set and sha not in integrated
    )
    work_pool = list(dict.fromkeys(work_pool))
    carry_pool = [
        sha for sha in dict.fromkeys(carry_pool) if sha not in integrated
    ]

    max_commits = int(policy.get("max_commits_per_run", 200))
    truncated = len(work_pool) > max_commits
    if truncated:
        warnings.append(
            f"Only the first {max_commits} new/retried commits were processed this "
            "run; the remainder will be picked up on the next scheduled run."
        )
        work_pool = work_pool[:max_commits]

    pool = carry_pool + work_pool
    pool_set = set(pool)
    classified: list[dict[str, Any]] = [
        classify_commit(sha, manifest, repo) for sha in pool
    ]

    included: list[dict[str, Any]] = []
    already_present: list[str] = []
    pending: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for commit in classified:
        if commit["decision"] != "candidate":
            pending.append(pending_entry(commit, commit["reason"]))

    eligible = [commit for commit in classified if commit["decision"] == "candidate"]
    candidate_created = False
    if eligible and args.dry_run:
        included = eligible
        candidate_created = True
        warnings.append(
            "Dry run: cherry-picks and conflict detection were not executed, and "
            "no branch or state was written."
        )
    elif eligible:
        git("switch", "-C", args.candidate_branch, base_sha, cwd=repo)
        for commit in eligible:
            sha = commit["sha"]
            outcome, detail = cherry_pick(sha, repo)
            if outcome == "picked":
                included.append(commit)
            elif outcome == "already_present":
                already_present.append(sha)
                warnings.append(
                    f"Upstream commit {short_sha(sha)} was already present in the "
                    "base branch; recorded as integrated."
                )
            else:
                reason = detail or "Cherry-pick conflict"
                conflicts.append(
                    {
                        "sha": sha,
                        "subject": commit.get("subject", ""),
                        "reason": reason,
                    }
                )
                pending.append(pending_entry(commit, reason))
        candidate_created = bool(included)
        git("switch", "--detach", base_sha, cwd=repo)

    requires_full_build = any(
        entry.get("requires_build") for entry in included
    )
    risk = overall_risk(included or pending)
    auto_merge_policy = bool(policy.get("auto_merge", False))
    automerge_eligible = bool(
        included
        and not pending
        and not requires_full_build
        and risk == "low"
        and auto_merge_policy
    )

    report = base_report(
        args,
        manifest,
        base_sha,
        target_sha,
        last_scanned,
        state,
        result="changes" if (included or pending) else "clean",
    )
    report.update(
        {
            "scanned_commits": len(pool),
            "included_shas": [entry["sha"] for entry in included],
            "pending_shas": [entry["sha"] for entry in pending],
            "already_present_shas": already_present,
            "carried_shas": carry_pool,
            "deferred_shas": [entry["sha"] for entry in state.get("deferred", [])],
            "commits_included": included,
            "commits_pending": pending,
            "conflicts": conflicts,
            "requires_full_build": requires_full_build,
            "risk": risk,
            "automerge_eligible": automerge_eligible,
            "candidate_created": candidate_created,
            "review_created": False,
            "review_file": None,
            "truncated": truncated,
            "warnings": warnings,
        }
    )
    for finding, commit in (
        (finding, commit)
        for commit in [*included, *pending]
        for finding in commit.get("patch_findings", [])
    ):
        finding.setdefault("commit", commit["sha"])

    review_file = REVIEW_DIR + f"/upstream-{short_sha(target_sha)}.md"
    if pending and not args.dry_run:
        git("switch", "-C", args.review_branch, base_sha, cwd=repo)
        write_text(repo / review_file, review_file_markdown(report))
        git("add", "--", review_file, cwd=repo)
        staged = git(
            "diff", "--cached", "--quiet", cwd=repo, check=False
        ).returncode
        if staged != 0:
            git(
                "commit",
                "-m",
                f"chore(upstream): review report for {short_sha(target_sha)}",
                cwd=repo,
            )
            report["review_created"] = True
            report["review_file"] = review_file
        else:
            warnings.append(
                "Review report was already present at the base commit; no review "
                "branch commit was created."
            )
        git("switch", "--detach", base_sha, cwd=repo)

    candidate_state_cleared = False
    if not args.dry_run:
        retried = set(pool) | (retry_set if args.retry_pending else set())
        update_deferred(state, retried)
        update_pending(state, retried, pending)

        state["processed_shas"] = list(
            dict.fromkeys([*state.get("processed_shas", []), *pool])
        )[-1000:]
        state["integrated_shas"] = list(
            dict.fromkeys(
                [*state.get("integrated_shas", []), *already_present]
            )
        )[-1000:]
        if included:
            state["candidate"] = {
                "target_sha": target_sha,
                "shas": [entry["sha"] for entry in included],
                "risk": overall_risk(included),
                "requires_full_build": requires_full_build,
                "created_at": now_utc(),
            }
        else:
            # No branch will be published. Clear any previous candidate so stale
            # SHAs cannot be carried forever after an upstream rewrite or a
            # candidate whose every commit was already present.
            if state.get("candidate") is not None:
                candidate_state_cleared = True
            state["candidate"] = None

        processed_in_range = [sha for sha in reachable if sha in pool_set]
        if truncated and processed_in_range:
            state["last_scanned_sha"] = processed_in_range[-1]
        else:
            state["last_scanned_sha"] = target_sha

    report["candidate_state_cleared"] = candidate_state_cleared
    report["next_steps"] = next_steps(
        included, pending, requires_full_build, args
    )
    finish(
        report,
        args,
        policy,
        state,
        state_changed=not args.dry_run,
    )
    return 0


def next_steps(
    included: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    requires_full_build: bool,
    args: argparse.Namespace,
) -> list[str]:
    steps: list[str] = []
    if included:
        steps.append(
            "Review the candidate PR. It contains only commits whose changed "
            "paths and patch.sh sections are candidate-eligible."
        )
    if requires_full_build:
        steps.append(
            "Run **Validate build** with mode `full` for the candidate head. "
            "The exact-tree marker is required by the `build-gate` check."
        )
    if pending:
        steps.append(
            "Resolve the review-only commits on a separate human branch. The "
            "automation review branch contains a report, never unresolved conflicts."
        )
    if not steps:
        steps.append("Nothing to do.")
    return steps


def base_report(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    base_sha: str,
    target_sha: str,
    last_scanned: str | None,
    state: dict[str, Any],
    result: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now_utc(),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "upstream": state.get("upstream", args.upstream),
        "base_sha": base_sha,
        "target_sha": target_sha,
        "last_scanned_before": last_scanned,
        "result": result,
        "candidate_branch": args.candidate_branch,
        "review_branch": args.review_branch,
        "manifest_schema": manifest.get("schema_version"),
        "state_snapshot": {
            "processed": len(state.get("processed_shas", [])),
            "integrated": len(state.get("integrated_shas", [])),
            "pending": len(state.get("pending", [])),
            "deferred": len(state.get("deferred", [])),
        },
    }


def finish(
    report: dict[str, Any],
    args: argparse.Namespace,
    policy: dict[str, Any],
    state: dict[str, Any],
    state_changed: bool,
) -> None:
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "change-report.json", report)
    markdown = report_markdown(report)
    write_text(report_dir / "change-report.md", markdown)

    if report.get("candidate_created"):
        write_text(report_dir / "candidate-pr.md", candidate_pr_body(report))
    if report.get("pending_shas"):
        write_text(report_dir / "review-pr.md", review_pr_body(report))
        write_text(report_dir / "review-file.md", review_file_markdown(report))

    if state_changed and not args.dry_run:
        history_entry = {
            "at": now_utc(),
            "run_id": report.get("run_id"),
            "target_sha": report.get("target_sha"),
            "result": report.get("result"),
            "included": report.get("included_shas", []),
            "pending": report.get("pending_shas", []),
            "risk": report.get("risk"),
        }
        state.setdefault("history", []).append(history_entry)
        save_state(
            args.state_file,
            state,
            max_history=int(policy.get("max_history_entries", 100)),
        )

    emit_github_output(
        {
            "result": report.get("result", ""),
            "target_sha": report.get("target_sha", ""),
            "included_shas": sha_list(report.get("included_shas", [])),
            "pending_shas": sha_list(report.get("pending_shas", [])),
            "candidate_created": str(
                bool(report.get("candidate_created"))
            ).lower(),
            "review_created": str(bool(report.get("review_created"))).lower(),
            "risk": report.get("risk", ""),
            "requires_full_build": str(
                bool(report.get("requires_full_build"))
            ).lower(),
            "state_changed": str(bool(state_changed)).lower(),
            "review_file": report.get("review_file") or "",
            "candidate_state_cleared": str(
                bool(report.get("candidate_state_cleared"))
            ).lower(),
        }
    )

    print(markdown)
    for warning in report.get("warnings", []):
        print(f"WARNING: {warning}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
