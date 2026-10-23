#!/usr/bin/env python3
"""Fail a pull request that changes protected UI without an explicit ack.

This script is run from the *base* branch by the workflow, so a pull request
cannot relax the manifest or replace the classifier that judges it.
"""

from __future__ import annotations

import argparse
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
    now_utc,
    short_sha,
    write_json,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument("--report-dir", default="ui-protection-report")
    parser.add_argument(
        "--acknowledged",
        action="store_true",
        help="the maintainer acknowledgement label is present on the PR",
    )
    return parser.parse_args(argv)


def annotation_escape(value: str) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def markdown(report: dict) -> str:
    counts = report["counts"]
    lines = [
        f"# UI protection: `{report['decision']}`",
        "",
        f"- Base: `{short_sha(report['base_sha'])}`",
        f"- Head: `{short_sha(report['head_sha'])}`",
        f"- Protected findings: **{counts['protected']}**",
        f"- Review-only findings: **{counts['review']}**",
        f"- Acknowledged by label: **{report['acknowledged']}**",
        "",
    ]
    findings = report["findings"]
    if findings:
        lines.append("| Protection | Path | Section | Reason |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings[:60]:
            path = finding["path"]
            section = finding.get("section") or ""
            reason = finding["reason"].replace("|", "\\|")
            lines.append(
                f"| `{finding['protection']}` | `{path}` | {section} | {reason} |"
            )
        if len(findings) > 60:
            lines.append("")
            lines.append(f"_{len(findings) - 60} more findings are in the JSON report._")
        lines.append("")

    if report["decision"] == "blocked":
        lines.append(
            "A maintainer must review the protected changes and add the "
            "`ui-protection:approved` label to acknowledge them. The label must "
            "not be used to skip review of the actual diff."
        )
    elif report["decision"] == "acknowledged":
        lines.append(
            "Protected changes are present and were acknowledged with the "
            "`ui-protection:approved` label."
        )
    elif report["decision"] == "review-required":
        lines.append(
            "No protected UI files changed. Build/CI/legal changes still require "
            "normal human review."
        )
    else:
        lines.append("No protected or review-only findings.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    manifest = load_manifest(repo / args.manifest)

    diff_base = git_out("merge-base", args.base, args.head, cwd=repo)
    if not diff_base:
        raise AutomationError(
            f"{args.base} and {args.head} have no common ancestor; cannot classify the pull request"
        )

    changes = diff_name_status(diff_base, args.head, repo)
    findings: list[dict] = []
    patch_files = set(manifest["patch_files"])

    for change in changes:
        paths = sorted({change["old_path"], change["path"]})
        for path in paths:
            rule = classify_path(path, manifest)
            if rule["protection"] in ("protected", "review"):
                findings.append(
                    {
                        "kind": "path",
                        "path": change["path"],
                        "old_path": change["old_path"],
                        "status": change["status"],
                        "class": rule["class"],
                        "protection": rule["protection"],
                        "section": None,
                        "reason": rule["reason"],
                    }
                )
            if path not in patch_files:
                continue
            for hunk in analyze_patch_change(
                diff_base, args.head, path, manifest, repo
            ):
                findings.append(
                    {
                        "kind": "patch-hunk",
                        "path": path,
                        "old_path": path,
                        "status": change["status"],
                        "class": "patch",
                        "protection": hunk["protection"],
                        "section": hunk.get("section"),
                        "reason": hunk["reason"],
                        "old_start": hunk.get("old_start"),
                        "new_start": hunk.get("new_start"),
                        "sample": hunk.get("sample", ""),
                    }
                )

    # A hunk-level finding supersedes the generic "section" path rule for the
    # same file; non-patch paths keep their path finding.
    findings = [
        finding
        for finding in findings
        if not (finding["kind"] == "path" and finding["path"] in patch_files)
    ]
    findings.sort(
        key=lambda finding: (
            {"protected": 0, "review": 1}.get(finding["protection"], 2),
            finding["path"],
            finding.get("new_start") or 0,
        )
    )
    protected = [f for f in findings if f["protection"] == "protected"]
    review = [f for f in findings if f["protection"] == "review"]

    if protected and args.acknowledged:
        decision = "acknowledged"
    elif protected:
        decision = "blocked"
    elif review:
        decision = "review-required"
    else:
        decision = "pass"

    report = {
        "schema_version": 1,
        "generated_at": now_utc(),
        "base_sha": args.base,
        "head_sha": args.head,
        "decision": decision,
        "acknowledged": bool(args.acknowledged),
        "counts": {"protected": len(protected), "review": len(review)},
        "findings": findings,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "ui-protection.json", report)
    summary = markdown(report)
    (report_dir / "ui-protection.md").write_text(summary, encoding="utf-8")
    append_step_summary(summary)

    for finding in findings:
        message = annotation_escape(f"{finding['reason']} ({finding['path']})")
        if finding["kind"] == "patch-hunk":
            location = (
                f"file={finding['path']},line={finding.get('new_start') or 1}"
            )
        else:
            location = f"file={finding['path']}"
        if finding["protection"] == "protected":
            level = "warning" if args.acknowledged else "error"
        else:
            level = "notice"
        print(f"::{level} {location}::{message}")

    print(summary)
    emit_github_output(
        {
            "decision": decision,
            "protected_count": len(protected),
            "review_count": len(review),
        }
    )
    return 1 if decision == "blocked" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
