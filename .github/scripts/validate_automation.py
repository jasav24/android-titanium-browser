#!/usr/bin/env python3
"""Static validation for the automation workflows, scripts and manifest.

The workflow runs this from the *base* branch against a worktree of the pull
request head. Python files are compiled, never imported or executed; shell
files are parsed with `bash -n`.
"""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

from automation_lib import (
    SECTION_HEADER_RE,
    AutomationError,
    _load_yaml,
    append_step_summary,
    git,
    load_manifest,
    write_json,
)

REQUIRED_WORKFLOWS = {
    "inspect-project.yml",
    "upstream-sync.yml",
    "ui-protection.yml",
    "upstream-review.yml",
    "validate-build.yml",
    "release.yml",
}
REQUIRED_DOCS = {
    ".github/automation/README.md",
    ".github/automation/TROUBLESHOOTING.md",
    ".github/automation/ROLLBACK.md",
    ".github/automation/UI-TESTING.md",
}
UNSAFE_RUN_PATTERNS = [
    re.compile(
        r"\$\{\{\s*(github\.event\.pull_request\.(title|body|head\.ref|head\.label)"
        r"|github\.head_ref|github\.event\.issue\.title|github\.event\.comment\.body)",
        re.IGNORECASE,
    )
]
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(?:#\s*(.*))?$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json-out", default="automation-validation.json")
    return parser.parse_args(argv)


def add(
    findings: list[dict[str, str]],
    severity: str,
    where: str,
    message: str,
) -> None:
    findings.append({"severity": severity, "where": where, "message": message})


def iter_steps(workflow: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                yield f"{job_name}/{step.get('name', 'step')}", step


def workflow_triggers(workflow: dict[str, Any]) -> set[str]:
    # PyYAML implements YAML 1.1, where the key `on` parses as boolean True.
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(item) for item in raw}
    return set()


def runs_on_hosted(job: dict[str, Any]) -> bool:
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return "self-hosted" in runs_on
    if isinstance(runs_on, list):
        return "self-hosted" in runs_on
    return False


def check_workflow_text(path: Path, findings: list[dict[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    where = path.name
    if "pull_request_target" in text:
        add(
            findings,
            "error",
            where,
            "pull_request_target is forbidden: it gives untrusted PR code a write token.",
        )
    if "secrets: inherit" in text:
        add(findings, "error", where, "secrets: inherit is forbidden.")

    for line in text.splitlines():
        match = USES_RE.match(line)
        if not match:
            continue
        value, comment = match.group(1), match.group(2) or ""
        if value.startswith("./") or value.startswith("docker://"):
            continue
        if "@" not in value:
            add(findings, "error", where, f"action is not pinned: {value}")
            continue
        _, ref = value.rsplit("@", 1)
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            add(
                findings,
                "error",
                where,
                f"action must use a 40-character commit SHA: {value}",
            )
        if not comment.strip():
            add(
                findings,
                "warning",
                where,
                f"pinned action has no version comment: {value}",
            )
    for match in re.finditer(
        r"git\s+push[^\n]*?--force(?!-with-lease)", text
    ):
        add(
            findings,
            "error",
            where,
            "git push --force is forbidden; use --force-with-lease: "
            + match.group(0),
        )


def check_workflow(
    path: Path, workflow: dict[str, Any], findings: list[dict[str, str]]
) -> None:
    where = path.name
    check_workflow_text(path, findings)
    if not isinstance(workflow, dict):
        add(findings, "error", where, "workflow is not a mapping")
        return
    triggers = workflow_triggers(workflow)
    if not triggers:
        add(findings, "error", where, "workflow has no `on` trigger")
    permissions = workflow.get("permissions")
    if permissions is None:
        add(findings, "error", where, "top-level permissions are required")
    elif permissions == "write-all":
        add(findings, "error", where, "permissions: write-all is forbidden")
    elif not isinstance(permissions, dict):
        add(
            findings,
            "error",
            where,
            "permissions must be an explicit mapping (or `{}` for none)",
        )
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        add(findings, "error", where, "workflow has no jobs")
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            add(findings, "error", where, f"job {job_name} is not a mapping")
            continue
        if job.get("permissions") == "write-all":
            add(findings, "error", where, f"job {job_name} uses write-all")
        if runs_on_hosted(job):
            if not job.get("environment"):
                add(
                    findings,
                    "error",
                    where,
                    f"self-hosted job {job_name} must be gated by an environment",
                )
            if not job.get("timeout-minutes"):
                add(
                    findings,
                    "warning",
                    where,
                    f"self-hosted job {job_name} has no timeout-minutes",
                )
    for step_name, step in iter_steps(workflow):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for pattern in UNSAFE_RUN_PATTERNS:
            if pattern.search(run):
                add(
                    findings,
                    "error",
                    where,
                    f"untrusted expression interpolated into run: in {step_name}",
                )
        if "git push" in run and "--force" in run and "--force-with-lease" not in run:
            add(
                findings,
                "error",
                where,
                f"unreviewable force push in {step_name}",
            )


def check_python(root: Path, findings: list[dict[str, str]]) -> None:
    scripts = root / ".github" / "scripts"
    files = sorted(scripts.glob("*.py")) + sorted(scripts.glob("tests/*.py"))
    if not files:
        add(findings, "error", ".github/scripts", "no Python scripts found")
        return
    with tempfile.TemporaryDirectory() as temp:
        for file in files:
            try:
                py_compile.compile(
                    str(file),
                    cfile=str(Path(temp) / (file.stem + ".pyc")),
                    doraise=True,
                )
            except py_compile.PyCompileError as exc:
                add(findings, "error", str(file.relative_to(root)), str(exc))


def check_shell(root: Path, findings: list[dict[str, str]]) -> None:
    bash = shutil.which("bash")
    if not bash:
        add(findings, "warning", "shell", "bash is not installed; skipped bash -n")
        return
    files = sorted(root.glob("*.sh")) + sorted(
        (root / ".github" / "scripts").glob("*.sh")
    )
    for file in files:
        result = subprocess.run(
            [bash, "-n", str(file)],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            add(
                findings,
                "error",
                str(file.relative_to(root)),
                result.stderr.strip() or "bash -n failed",
            )


def check_patch_manifest(
    root: Path, manifest: dict[str, Any], findings: list[dict[str, str]]
) -> None:
    patch = root / "patch.sh"
    if patch.is_file():
        headers = []
        for line in patch.read_text(encoding="utf-8").splitlines():
            match = SECTION_HEADER_RE.match(line.rstrip())
            if match:
                headers.append(match.group(1).strip())
        declared = [entry["header"] for entry in manifest["patch_sections"]]
        for header in headers:
            if header not in declared:
                add(
                    findings,
                    "error",
                    "patch.sh",
                    f"section {header!r} is missing from the manifest",
                )
        for header in declared:
            if header not in headers:
                add(
                    findings,
                    "error",
                    "ui-protection.yml",
                    f"section {header!r} no longer exists in patch.sh",
                )
    else:
        add(findings, "error", "patch.sh", "patch.sh does not exist")


def validate_repository(root: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    try:
        manifest = load_manifest(root / ".github" / "ui-protection.yml")
    except AutomationError as exc:
        add(findings, "error", ".github/ui-protection.yml", str(exc))
        manifest = None

    workflows_dir = root / ".github" / "workflows"
    workflow_paths = sorted(workflows_dir.glob("*.yml"))
    names = {path.name for path in workflow_paths}
    for name in sorted(REQUIRED_WORKFLOWS - names):
        add(findings, "error", ".github/workflows", f"missing workflow: {name}")
    if (workflows_dir / "build.yml").exists():
        add(
            findings,
            "error",
            ".github/workflows/build.yml",
            "the legacy workflow auto-pushes to main and publishes without a "
            "candidate gate; it must be replaced by release.yml",
        )
    for path in workflow_paths:
        try:
            workflow = _load_yaml(path)
        except AutomationError as exc:
            add(findings, "error", path.name, str(exc))
            continue
        check_workflow(path, workflow, findings)

    check_python(root, findings)
    check_shell(root, findings)
    if manifest is not None:
        check_patch_manifest(root, manifest, findings)

    for doc in sorted(REQUIRED_DOCS):
        if not (root / doc).is_file():
            add(findings, "warning", doc, "documentation file is missing")

    schema = root / ".github" / "automation" / "change-report.schema.json"
    if not schema.is_file():
        add(findings, "warning", str(schema), "change report schema is missing")
    else:
        try:
            json.loads(schema.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            add(findings, "error", str(schema), f"invalid JSON: {exc}")

    errors = [item for item in findings if item["severity"] == "error"]
    warnings = [item for item in findings if item["severity"] == "warning"]
    return {
        "schema_version": 1,
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.repo).resolve()
    report = validate_repository(root)
    report["repo"] = str(root)

    output = Path(args.json_out)
    if not output.is_absolute():
        output = Path.cwd() / output
    write_json(output, report)
    markdown = [
        f"# Automation validation: {'passed' if report['ok'] else 'failed'}",
        "",
        f"- Errors: **{report['error_count']}**",
        f"- Warnings: **{report['warning_count']}**",
        "",
    ]
    if report["findings"]:
        markdown.extend(
            ["| Severity | Where | Message |", "| --- | --- | --- |"]
        )
        for item in report["findings"]:
            message = item["message"].replace("|", "\\|").replace("\n", " ")
            markdown.append(
                f"| {item['severity']} | `{item['where']}` | {message} |"
            )
    markdown.append("")
    summary = "\n".join(markdown)
    append_step_summary(summary)
    print(summary)
    for item in report["findings"]:
        if item["severity"] == "error":
            print(
                f"ERROR: {item['where']}: {item['message']}", file=sys.stderr
            )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
