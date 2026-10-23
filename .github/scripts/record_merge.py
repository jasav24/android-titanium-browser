#!/usr/bin/env python3
"""Record a merged automation PR in the durable state.

Candidate PR: its included upstream SHAs become integrated.
Review PR: its acknowledged pending SHAs become deferred; explicit
`Upstream-SHA:` lines on any merged PR become integrated.

The workflow that runs this script checks out the *base* branch, so PR code is
never executed here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from automation_lib import (
    AutomationError,
    append_step_summary,
    load_manifest,
    load_state,
    now_utc,
    parse_pr_markers,
    save_state,
    short_sha,
    write_json,
    write_text,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--merged",
        action="store_true",
        help="override the event payload (used after an in-workflow merge)",
    )
    parser.add_argument("--merge-commit", default="", help="override merge SHA")
    return parser.parse_args(argv)


def load_event(path: str | None) -> dict[str, Any]:
    if not path:
        raise AutomationError("no pull_request event file supplied")
    file = Path(path)
    if not file.is_file():
        raise AutomationError(f"event file does not exist: {file}")
    data = json.loads(file.read_text(encoding="utf-8"))
    if "pull_request" not in data:
        raise AutomationError("event payload has no pull_request object")
    return data


def markdown(record: dict[str, Any]) -> str:
    lines = [
        f"# Merge recorded: PR #{record['pr']}",
        "",
        f"- PR: {record['url']}",
        f"- Kind: `{record.get('kind') or 'manual'}`",
        f"- Merge commit: `{short_sha(record.get('merge_commit_sha'))}`",
        f"- Upstream target: `{record.get('target_sha') or 'n/a'}`",
        f"- Newly integrated: {len(record['newly_integrated'])}",
        f"- Newly deferred: {len(record['newly_deferred'])}",
        f"- Candidate state cleared: {record['candidate_cleared']}",
        "",
    ]
    if record["newly_integrated"]:
        lines.append("## Integrated")
        lines.append("")
        lines.extend(f"- `{sha}`" for sha in record["newly_integrated"])
        lines.append("")
    if record["newly_deferred"]:
        lines.append("## Deferred after review acknowledgement")
        lines.append("")
        lines.extend(f"- `{sha}`" for sha in record["newly_deferred"])
        lines.append("")
    lines.append(
        "Deferred commits are not retried automatically. Remove the SHA from "
        "`state.json` or run a manual sync with retry to reconsider them."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    event = load_event(args.event)
    pr = event["pull_request"]
    merged = bool(args.merged or pr.get("merged"))
    number = int(pr.get("number") or 0)
    body = pr.get("body") or ""
    markers = parse_pr_markers(body)
    managed = markers["kind"] is not None or bool(markers["explicit_integrated"])

    if not merged or not managed:
        print(
            f"PR #{number}: merged={merged} managed={managed}; nothing to record."
        )
        return 0

    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    state = load_state(state_path, "upstream")
    merge_sha = args.merge_commit or pr.get("merge_commit_sha") or ""
    merge_key = f"pr:{number}:{merge_sha or pr.get('merged_at') or ''}"

    record_path = state_dir / "merges" / f"pr-{number}.json"
    if record_path.exists():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("merge_key") == merge_key:
            print(f"PR #{number}: merge {merge_key} was already recorded; skipping.")
            return 0
    if any(
        isinstance(entry, dict) and entry.get("merge_key") == merge_key
        for entry in state.get("history", [])
    ):
        print(f"PR #{number}: merge {merge_key} is in history; skipping.")
        return 0

    integrated = set(state.get("integrated_shas", []))
    pending = {
        entry["sha"]: entry for entry in state.get("pending", []) if entry.get("sha")
    }
    deferred = {
        entry["sha"]: entry for entry in state.get("deferred", []) if entry.get("sha")
    }

    newly_integrated: list[str] = []
    newly_deferred: list[str] = []
    acknowledged: list[str] = []

    if markers["kind"] == "sync":
        for sha in markers["included"]:
            if sha not in integrated:
                integrated.add(sha)
                newly_integrated.append(sha)
    if markers["kind"] == "review":
        for sha in markers["review_pending"]:
            acknowledged.append(sha)
            if sha in markers["explicit_integrated"]:
                if sha not in integrated:
                    integrated.add(sha)
                    newly_integrated.append(sha)
            elif sha in pending:
                entry = dict(pending.pop(sha))
                entry["reason"] = (
                    f"Acknowledged by merging review PR #{number}; not integrated "
                    "automatically."
                )
                entry["deferred_at"] = now_utc()
                entry["deferred_by_pr"] = number
                deferred.setdefault(sha, entry)
                newly_deferred.append(sha)
    for sha in markers["explicit_integrated"]:
        if sha not in integrated:
            integrated.add(sha)
            newly_integrated.append(sha)

    for sha in list(integrated):
        pending.pop(sha, None)
        deferred.pop(sha, None)

    candidate_cleared = False
    candidate = state.get("candidate")
    if markers["kind"] == "sync" and isinstance(candidate, dict):
        # Only commits confirmed by this merge leave the candidate. Clearing the
        # whole candidate on a target match could lose commits that were not in
        # the merged head.
        remaining = [
            sha for sha in candidate.get("shas", []) if sha not in integrated
        ]
        candidate["shas"] = remaining
        if not remaining:
            state["candidate"] = None
            candidate_cleared = True

    record = {
        "schema_version": 1,
        "merge_key": merge_key,
        "pr": number,
        "title": pr.get("title", ""),
        "url": pr.get("html_url", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": merge_sha,
        "head_ref": (pr.get("head") or {}).get("ref", ""),
        "base_ref": (pr.get("base") or {}).get("ref", ""),
        "kind": markers["kind"],
        "target_sha": markers["target"],
        "included": markers["included"],
        "acknowledged": acknowledged,
        "explicit_integrated": markers["explicit_integrated"],
        "newly_integrated": newly_integrated,
        "newly_deferred": newly_deferred,
        "candidate_cleared": candidate_cleared,
        "recorded_at": now_utc(),
    }

    summary = markdown(record)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / f"merge-{number}.json", record)
    write_text(report_dir / f"merge-{number}.md", summary)
    append_step_summary(summary)
    print(summary)

    if args.dry_run:
        print("Dry run: state was not written.")
        return 0

    state["integrated_shas"] = list(
        dict.fromkeys([*state.get("integrated_shas", []), *newly_integrated])
    )[-2000:]
    state["pending"] = list(pending.values())
    state["deferred"] = list(deferred.values())
    state.setdefault("history", []).append(
        {
            "at": now_utc(),
            "result": "merge",
            "merge_key": merge_key,
            "pr": number,
            "merge_sha": merge_sha,
            "kind": markers["kind"],
            "included": newly_integrated,
            "deferred": newly_deferred,
        }
    )

    manifest = load_manifest(Path.cwd() / args.manifest)
    max_history = int(manifest["policy"].get("max_history_entries", 100))
    state_dir.mkdir(parents=True, exist_ok=True)
    save_state(state_path, state, max_history=max_history)
    record_dir = state_dir / "merges"
    record_dir.mkdir(parents=True, exist_ok=True)
    write_json(record_path, record)
    print(
        f"Recorded merge for PR #{number}: "
        f"{len(newly_integrated)} integrated, {len(newly_deferred)} deferred."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
