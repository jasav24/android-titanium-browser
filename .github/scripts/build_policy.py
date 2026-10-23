#!/usr/bin/env python3
"""Decide whether a diff needs a full Chromium build or only lightweight checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from automation_lib import (
    AutomationError,
    analyze_patch_change,
    append_step_summary,
    classify_path,
    diff_name_status,
    emit_github_output,
    git_out,
    load_manifest,
    short_sha,
    write_json,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument("--report-dir", default="build-policy-report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    manifest = load_manifest(repo / args.manifest)
    patch_files = set(manifest["patch_files"])

    diff_base = git_out("merge-base", args.base, args.head, cwd=repo)
    if not diff_base:
        raise AutomationError(
            f"{args.base} and {args.head} have no common ancestor; cannot classify the change"
        )

    changes = diff_name_status(diff_base, args.head, repo)
    paths: list[dict] = []
    reasons: list[str] = []
    build_required = False
    protected = False
    for change in changes:
        inspected = sorted({change["old_path"], change["path"]})
        rules = [classify_path(path, manifest) for path in inspected]
        rule = max(
            rules,
            key=lambda value: {"candidate": 0, "section": 1, "review": 2, "protected": 3}.get(
                value["protection"], 2
            ),
        )
        paths.append(
            {
                "status": change["status"],
                "path": change["path"],
                "old_path": change["old_path"],
                "class": rule["class"],
                "protection": rule["protection"],
                "requires_build": bool(rule.get("requires_build")),
                "reason": rule["reason"],
            }
        )
        if rule.get("requires_build") and change["path"] not in patch_files:
            build_required = True
            reasons.append(f"{change['path']}: {rule['reason']}")
        if rule["protection"] == "protected":
            protected = True
        if change["path"] in patch_files:
            build_required = True
            findings = analyze_patch_change(
                diff_base, args.head, change["path"], manifest, repo
            )
            sections = sorted(
                {
                    (finding.get("section") or "unlabeled")
                    for finding in findings
                    if finding["protection"] in ("protected", "review")
                }
            )
            if sections:
                reasons.append(
                    "patch.sh UI/feature sections changed: " + ", ".join(sections)
                )
            else:
                reasons.append("patch.sh changed; a full build validates the sed edits.")

    reasons = list(dict.fromkeys(reasons))
    risk = "high" if protected else ("medium" if build_required else "low")
    report = {
        "schema_version": 1,
        "base_sha": args.base,
        "diff_base_sha": diff_base,
        "head_sha": args.head,
        "full_build_required": build_required,
        "risk": risk,
        "reasons": reasons,
        "paths": paths,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "build-policy.json", report)
    summary_lines = [
        "# Build policy",
        "",
        f"- Base: `{short_sha(args.base)}`",
        f"- Head: `{short_sha(args.head)}`",
        f"- Full Chromium build required: **{build_required}**",
        "",
    ]
    if reasons:
        summary_lines.extend(f"- {reason}" for reason in reasons[:30])
    if not build_required:
        summary_lines.append(
            "- Lightweight validation is sufficient for this change set."
        )
    summary_lines.append("")
    summary = "\n".join(summary_lines)
    (report_dir / "build-policy.md").write_text(summary, encoding="utf-8")
    append_step_summary(summary)
    print(summary)
    print(json.dumps(report, indent=2))
    emit_github_output(
        {
            "full_build_required": str(build_required).lower(),
            "risk": risk,
            "reason": "; ".join(reasons)[:500],
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
