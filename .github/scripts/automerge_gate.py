#!/usr/bin/env python3
"""Gate the optional controlled auto-merge of an upstream candidate PR.

This is intentionally redundant: the manifest must opt in, the repository
variable must be enabled, a maintainer must add the label, the environment
must be approved, the state branch must still describe exactly this PR, and
the UI protection, lightweight validation and build-gate checks must all have
succeeded for this commit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from automation_lib import (
    AutomationError,
    load_manifest,
    load_state,
    parse_pr_markers,
    short_sha,
)

REQUIRED_CHECKS = ("ui-protection", "lightweight-validation", "build-gate")
ALLOWED_PERMISSIONS = {"admin", "maintain", "write"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument(
        "--enabled",
        default=os.environ.get("UPSTREAM_AUTOMERGE_ENABLED", ""),
        help="must be the string 'true'",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


class GateError(RuntimeError):
    pass


def api(method: str, url: str, token: str) -> dict:
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GateError(
            f"GitHub API {method} {url} failed with {exc.code}: {detail}"
        ) from exc


def latest_checks(repo: str, sha: str, token: str, base: str) -> dict[str, dict]:
    data = api(
        "GET",
        f"{base}/repos/{repo}/commits/{sha}/check-runs?filter=all&per_page=100",
        token,
    )
    latest: dict[str, dict] = {}
    for run in data.get("check_runs", []):
        name = run.get("name")
        if not name:
            continue
        current = latest.get(name)
        if current is None or run.get("id", 0) > current.get("id", 0):
            latest[name] = run
    return latest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems: list[str] = []

    if args.enabled != "true":
        problems.append(
            "repository variable UPSTREAM_AUTOMERGE_ENABLED is not 'true'"
        )
    manifest = load_manifest(Path.cwd() / args.manifest)
    if not bool(manifest["policy"].get("auto_merge", False)):
        problems.append(
            "policy.auto_merge is false in .github/ui-protection.yml"
        )

    if not args.event:
        raise AutomationError("no pull request event file supplied")
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    body = pr.get("body") or ""
    markers = parse_pr_markers(body)
    actor = (event.get("sender") or {}).get("login") or ""
    if not actor:
        problems.append("cannot identify the label actor")
    if markers["kind"] != "sync":
        problems.append("PR is not an upstream-sync candidate PR")
    if not markers["target"]:
        problems.append("PR body has no upstream target marker")
    if not markers["automerge"]:
        problems.append("PR body does not mark this change as automerge-eligible")
    if markers["sync_pending"]:
        problems.append("candidate PR reports pending review commits")
    if not markers["included"]:
        problems.append("candidate PR has no included commits")

    state = load_state(
        Path(args.state_dir) / "state.json", "upstream"
    )
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        problems.append("state branch has no open candidate")
    else:
        if candidate.get("target_sha") != markers["target"]:
            problems.append(
                "state candidate target does not match the PR target"
            )
        if set(candidate.get("shas", [])) != set(markers["included"]):
            problems.append(
                "state candidate commits do not match the PR included commits"
            )
        if candidate.get("risk") != "low":
            problems.append(
                f"candidate risk is {candidate.get('risk')}, not low"
            )
        if candidate.get("requires_full_build"):
            problems.append("candidate requires a full build")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    checks: dict[str, dict] = {}
    pull: dict = {}
    if not token:
        problems.append("GITHUB_TOKEN is not set")
    else:
        try:
            pull = api(
                "GET", f"{base}/repos/{args.repo}/pulls/{args.pr_number}", token
            )
            repository = api("GET", f"{base}/repos/{args.repo}", token)
            default_branch = repository.get("default_branch")
            if pull.get("state") != "open":
                problems.append("PR is not open")
            if pull.get("merged"):
                problems.append("PR is already merged")
            if pull.get("draft"):
                problems.append("PR is still a draft")
            if ((pull.get("head") or {}).get("sha")) != args.head_sha:
                problems.append("PR head SHA changed during verification")
            if args.base_ref != default_branch:
                problems.append(
                    f"PR targets {args.base_ref!r}, not the default branch "
                    f"{default_branch!r}"
                )
            labels = {
                label.get("name")
                for label in pull.get("labels", [])
                if isinstance(label, dict)
            }
            if "automation-automerge" not in labels:
                problems.append(
                    "the automation-automerge label was removed during verification"
                )
            base_repo = ((pull.get("base") or {}).get("repo") or {}).get(
                "full_name"
            )
            head_repo = ((pull.get("head") or {}).get("repo") or {}).get(
                "full_name"
            )
            if base_repo != args.repo:
                problems.append("PR does not target this repository")
            if head_repo != args.repo:
                problems.append("PR does not come from a branch in this repository")

            for attempt in range(5):
                if pull.get("mergeable") is not None:
                    break
                if attempt < 4:
                    time.sleep(2)
                pull = api(
                    "GET",
                    f"{base}/repos/{args.repo}/pulls/{args.pr_number}",
                    token,
                )
            if pull.get("mergeable") is not True:
                problems.append("GitHub reports the PR as not mergeable")
            if pull.get("mergeable_state") not in ("clean", "unstable"):
                problems.append(
                    f"mergeable state is {pull.get('mergeable_state')!r}"
                )

            if actor:
                permission = api(
                    "GET",
                    f"{base}/repos/{args.repo}/collaborators/{actor}/permission",
                    token,
                )
                if permission.get("permission") not in ALLOWED_PERMISSIONS:
                    problems.append(
                        f"label actor {actor!r} has permission "
                        f"{permission.get('permission')!r}, not write or better"
                    )

            checks = latest_checks(args.repo, args.head_sha, token, base)
            for name in REQUIRED_CHECKS:
                run = checks.get(name)
                if not run:
                    problems.append(f"required check {name!r} has not run")
                elif run.get("status") != "completed":
                    problems.append(f"required check {name!r} is not complete")
                elif run.get("conclusion") != "success":
                    problems.append(
                        f"required check {name!r} concluded "
                        f"{run.get('conclusion')!r}"
                    )
        except GateError as exc:
            problems.append(str(exc))

    result = {
        "pr": args.pr_number,
        "head_sha": args.head_sha,
        "target": markers["target"],
        "included": markers["included"],
        "actor": actor,
        "checks": {
            name: {
                "present": name in checks,
                "status": checks.get(name, {}).get("status"),
                "conclusion": checks.get(name, {}).get("conclusion"),
            }
            for name in REQUIRED_CHECKS
        },
        "problems": problems,
        "allowed": not problems,
    }
    print(json.dumps(result, indent=2))
    if problems:
        print("\nAuto-merge blocked:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print(
        f"Auto-merge allowed for PR #{args.pr_number} "
        f"({short_sha(args.head_sha)})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
