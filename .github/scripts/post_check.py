#!/usr/bin/env python3
"""Create or update the exact-tree `build-gate` check run.

The check is keyed by commit SHA and name. If a check with the same name
already exists (for example an earlier failed attempt), it is updated instead
of creating a duplicate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

CONCLUSIONS = {
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--name", default="build-gate")
    parser.add_argument("--conclusion", required=True, choices=sorted(CONCLUSIONS))
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--summary-file")
    parser.add_argument("--details-url", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def api(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API {method} {url} failed with {exc.code}: {detail}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not token and not args.dry_run:
        print("ERROR: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    summary = args.summary
    if args.summary_file:
        summary = open(args.summary_file, encoding="utf-8").read()
    summary = summary[:65535]
    output = {"title": args.title[:255], "summary": summary}
    payload: dict = {
        "name": args.name,
        "head_sha": args.sha,
        "status": "completed",
        "conclusion": args.conclusion,
        "output": output,
    }
    if args.details_url:
        payload["details_url"] = args.details_url

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    repo = args.repo
    listing = api(
        "GET",
        f"{base}/repos/{repo}/commits/{args.sha}/check-runs"
        f"?check_name={args.name}&filter=all&per_page=20",
        token,
    )
    runs = [
        run
        for run in listing.get("check_runs", [])
        if run.get("name") == args.name
        and (run.get("app") or {}).get("slug") == "github-actions"
    ]
    runs.sort(key=lambda run: run.get("id", 0), reverse=True)
    if runs:
        updated = api(
            "PATCH",
            f"{base}/repos/{repo}/check-runs/{runs[0]['id']}",
            token,
            payload,
        )
        print(f"updated check run: {updated.get('html_url', '')}")
        return 0

    created = api(
        "POST", f"{base}/repos/{repo}/check-runs", token, payload
    )
    print(f"created check run: {created.get('html_url', '')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
