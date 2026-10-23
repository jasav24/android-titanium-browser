#!/usr/bin/env python3
"""Shared helpers for the Titanium upstream-sync automation.

Only the Python standard library is required. YAML is parsed with PyYAML when
available and otherwise converted through Ruby's stdlib Psych, which is present
on GitHub-hosted Ubuntu runners.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ".github/ui-protection.yml"
STATE_BRANCH = "automation/state"
CANDIDATE_BRANCH = "automation/upstream-candidate"
REVIEW_BRANCH = "automation/upstream-review"
STATE_FILE = "state.json"
REVIEW_DIR = ".github/upstream-sync/reviews"
REPORT_ARTIFACT_DIR = ".github/upstream-sync/reports"

PROTECTION_RANK = {"candidate": 0, "review": 1, "protected": 2, "section": 1}
VALID_PROTECTIONS = {"protected", "review", "candidate", "section"}
VALID_CLASSES = {
    "protected",
    "patch",
    "build",
    "ci",
    "security",
    "dependency",
    "docs",
    "legal",
    "unknown",
}

SECTION_HEADER_RE = re.compile(
    r"^#\s+((?:ext|desktop|dev|accessibility|playback|brand|links)(?::\s*\S.*)?)\s*$"
)
HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


class AutomationError(RuntimeError):
    """Raised when an automation precondition or git command fails."""


# ---------------------------------------------------------------------------
# Time, process and git helpers
# ---------------------------------------------------------------------------


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def run(
    cmd: list[str],
    cwd: str | Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    merged_env = None
    if env is not None:
        merged_env = dict(os.environ)
        merged_env.update(env)
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        env=merged_env,
    )
    if check and result.returncode != 0:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        raise AutomationError(
            "command failed with exit code "
            f"{result.returncode}: {' '.join(cmd)}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )
    return result


def git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=cwd, check=check, env=env)


def git_out(*args: str, cwd: str | Path | None = None) -> str:
    return git(*args, cwd=cwd).stdout.strip()


def rev_parse(ref: str, cwd: str | Path | None = None) -> str:
    return git_out("rev-parse", "--verify", ref, cwd=cwd)


def tree_sha(ref: str, cwd: str | Path | None = None) -> str:
    return git_out("rev-parse", f"{ref}^{{tree}}", cwd=cwd)


def is_ancestor(ancestor: str, descendant: str, cwd: str | Path | None = None) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant, cwd=cwd, check=False).returncode == 0


def ensure_clean_worktree(cwd: str | Path) -> None:
    dirty = git_out("status", "--porcelain", cwd=cwd)
    if dirty:
        raise AutomationError(
            "working tree is not clean; refusing to create automation branches:\n" + dirty
        )


# ---------------------------------------------------------------------------
# YAML / JSON
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass

    ruby = shutil.which("ruby")
    if ruby:
        command = [
            ruby,
            "-ryaml",
            "-rjson",
            "-e",
            (
                "data = YAML.safe_load_file(ARGV[0]); "
                "abort('YAML document is empty') if data.nil?; "
                "print JSON.generate(data)"
            ),
            str(path),
        ]
        result = run(command)
        return json.loads(result.stdout)

    # A strict-JSON document is also valid YAML; this keeps local use working
    # in minimal environments.
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutomationError(
            f"cannot parse {path}: install PyYAML or Ruby (Psych), "
            "or write the manifest in JSON syntax"
        ) from exc


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = Path.cwd() / manifest_path
    data = _load_yaml(manifest_path)
    validate_manifest(data, manifest_path)
    return data


def validate_manifest(data: Any, source: str | Path = "manifest") -> None:
    where = str(source)
    if not isinstance(data, dict):
        raise AutomationError(f"{where}: manifest must be a mapping")
    for key in ("schema_version", "upstream", "policy", "path_rules", "patch_files", "patch_sections"):
        if key not in data:
            raise AutomationError(f"{where}: missing required key {key!r}")
    if data["schema_version"] != 1:
        raise AutomationError(f"{where}: unsupported schema_version {data['schema_version']!r}")

    policy = data["policy"]
    for key in ("default_path_protection", "default_patch_protection", "state_branch", "candidate_branch", "review_branch"):
        if key not in policy:
            raise AutomationError(f"{where}: missing policy.{key}")
    if policy["default_path_protection"] not in VALID_PROTECTIONS:
        raise AutomationError(f"{where}: invalid policy.default_path_protection")
    if policy["default_patch_protection"] not in VALID_PROTECTIONS:
        raise AutomationError(f"{where}: invalid policy.default_patch_protection")

    rules = data["path_rules"]
    if not isinstance(rules, list) or not rules:
        raise AutomationError(f"{where}: path_rules must be a non-empty list")
    if rules[-1].get("pattern") != "**":
        raise AutomationError(f"{where}: path_rules must end with a '**' default-deny rule")
    seen_patterns: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise AutomationError(f"{where}: path_rules[{index}] must be a mapping")
        for key in ("pattern", "class", "protection", "reason"):
            if not rule.get(key):
                raise AutomationError(f"{where}: path_rules[{index}] missing {key}")
        if rule["pattern"] in seen_patterns:
            raise AutomationError(f"{where}: duplicate path pattern {rule['pattern']!r}")
        seen_patterns.add(rule["pattern"])
        if rule["protection"] not in VALID_PROTECTIONS:
            raise AutomationError(f"{where}: path_rules[{index}] invalid protection")
        if rule["class"] not in VALID_CLASSES:
            raise AutomationError(f"{where}: path_rules[{index}] invalid class {rule['class']!r}")
        if not isinstance(rule.get("requires_build", False), bool):
            raise AutomationError(f"{where}: path_rules[{index}].requires_build must be boolean")

    patch_files = data["patch_files"]
    if not isinstance(patch_files, list) or not patch_files:
        raise AutomationError(f"{where}: patch_files must be a non-empty list")
    for value in patch_files:
        if not isinstance(value, str) or not value:
            raise AutomationError(f"{where}: invalid patch_files entry")

    sections = data["patch_sections"]
    if not isinstance(sections, list):
        raise AutomationError(f"{where}: patch_sections must be a list")
    seen_headers: set[str] = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise AutomationError(f"{where}: patch_sections[{index}] must be a mapping")
        for key in ("header", "protection", "reason"):
            if not section.get(key):
                raise AutomationError(f"{where}: patch_sections[{index}] missing {key}")
        if section["header"] in seen_headers:
            raise AutomationError(f"{where}: duplicate section header {section['header']!r}")
        seen_headers.add(section["header"])
        if section["protection"] not in VALID_PROTECTIONS:
            raise AutomationError(f"{where}: patch_sections[{index}] invalid protection")

    if not isinstance(data.get("build", {}), dict):
        raise AutomationError(f"{where}: build must be a mapping")
    if not isinstance(data.get("security_keywords", []), list):
        raise AutomationError(f"{where}: security_keywords must be a list")


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def read_json(path: str | Path, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


# ---------------------------------------------------------------------------
# Path and patch.sh classification
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def path_matches(path: str, pattern: str) -> bool:
    path = normalize_path(path)
    pattern = normalize_path(pattern)
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern


def classify_path(path: str, manifest: dict[str, Any]) -> dict[str, Any]:
    for rule in manifest["path_rules"]:
        if path_matches(path, rule["pattern"]):
            return dict(rule)
    return {
        "pattern": "**",
        "class": "unknown",
        "protection": manifest["policy"]["default_path_protection"],
        "requires_build": True,
        "reason": "Unclassified path.",
    }


def diff_name_status(base: str, head: str, cwd: str | Path) -> list[dict[str, str]]:
    output = git(
        "diff",
        "--name-status",
        "--find-renames",
        "-z",
        base,
        head,
        cwd=cwd,
    ).stdout
    parts = output.split("\0")
    records: list[dict[str, str]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            break
        if status[0] in ("R", "C"):
            old_path = parts[index]
            new_path = parts[index + 1]
            index += 2
            records.append({"status": status, "old_path": old_path, "path": new_path})
        else:
            path = parts[index]
            index += 1
            records.append({"status": status, "old_path": path, "path": path})
    return records


def parse_patch_hunks(diff_text: str) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        match = HUNK_RE.match(line)
        if match:
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2) or 1),
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4) or 1),
                "section": match.group(5).strip(),
                "lines": [],
            }
        elif current is not None:
            current["lines"].append(line)
    if current is not None:
        hunks.append(current)
    return hunks


def section_at(lines: list[str], index: int) -> str | None:
    if not lines:
        return None
    index = min(max(index, 0), len(lines) - 1)
    for cursor in range(index, -1, -1):
        match = SECTION_HEADER_RE.match(lines[cursor].rstrip())
        if match:
            return match.group(1).strip()
    return None


def _section_rule(
    header: str | None, manifest: dict[str, Any]
) -> dict[str, Any]:
    if header is None:
        return {
            "header": None,
            "protection": manifest["policy"]["default_patch_protection"],
            "reason": "Unlabeled patch.sh region; no section header was declared.",
        }
    for section in manifest["patch_sections"]:
        if section["header"] == header:
            return dict(section)
    return {
        "header": header,
        "protection": "review",
        "reason": "Section header is not listed in the manifest.",
    }


def _worst_rule(rules: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return max(rules, key=lambda rule: PROTECTION_RANK.get(rule["protection"], 1))


def analyze_patch_diff(
    diff_text: str,
    base_lines: list[str],
    head_lines: list[str],
    manifest: dict[str, Any],
    file_path: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for hunk in parse_patch_hunks(diff_text):
        changed = [line for line in hunk["lines"] if line[:1] in ("+", "-")]
        header_changed = any(
            SECTION_HEADER_RE.match(line[1:].rstrip()) for line in changed
        )

        sections: set[str] = set()
        if hunk["new_count"] > 0:
            start = max(hunk["new_start"] - 1, 0)
            end = min(start + hunk["new_count"], len(head_lines))
            for index in range(start, max(end, start + 1)):
                header = section_at(head_lines, index)
                if header:
                    sections.add(header)
        if hunk["old_count"] > 0:
            start = max(hunk["old_start"] - 1, 0)
            end = min(start + hunk["old_count"], len(base_lines))
            for index in range(start, max(end, start + 1)):
                header = section_at(base_lines, index)
                if header:
                    sections.add(header)
        if not sections:
            fallback = section_at(
                head_lines, max(hunk["new_start"] - 1, 0)
            ) or section_at(base_lines, max(hunk["old_start"] - 1, 0))
            if fallback:
                sections.add(fallback)

        rules = [_section_rule(header, manifest) for header in sorted(sections)]
        if not rules:
            rules = [_section_rule(None, manifest)]
        worst = _worst_rule(rules)
        protection = worst["protection"]
        reason = worst["reason"]
        if header_changed:
            protection = "protected"
            reason = "A patch.sh section header changed; classification itself is affected."
        elif len(rules) > 1:
            reason = "Hunk spans multiple sections: " + ", ".join(
                sorted(sections) or ["unlabeled"]
            )

        sample = next((line[1:].strip() for line in changed if line[1:].strip()), "")
        findings.append(
            {
                "file": file_path,
                "old_start": hunk["old_start"],
                "old_count": hunk["old_count"],
                "new_start": hunk["new_start"],
                "new_count": hunk["new_count"],
                "sections": sorted(sections),
                "section": worst.get("header"),
                "protection": protection,
                "reason": reason,
                "header_changed": header_changed,
                "sample": sample[:240],
            }
        )
    return findings


def file_at(ref: str, path: str, cwd: str | Path) -> list[str]:
    result = git("show", f"{ref}:{path}", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def analyze_patch_change(
    base_ref: str,
    head_ref: str,
    file_path: str,
    manifest: dict[str, Any],
    cwd: str | Path,
) -> list[dict[str, Any]]:
    base_lines = file_at(base_ref, file_path, cwd)
    head_lines = file_at(head_ref, file_path, cwd)
    if not base_lines and head_lines:
        return [
            {
                "file": file_path,
                "old_start": 0,
                "old_count": 0,
                "new_start": 1,
                "new_count": len(head_lines),
                "sections": [],
                "section": None,
                "protection": "protected",
                "reason": "The patch file was added; it must start from review.",
                "header_changed": False,
                "sample": "",
            }
        ]
    if base_lines and not head_lines:
        return [
            {
                "file": file_path,
                "old_start": 1,
                "old_count": len(base_lines),
                "new_start": 0,
                "new_count": 0,
                "sections": [],
                "section": None,
                "protection": "protected",
                "reason": "The patch file was deleted.",
                "header_changed": False,
                "sample": "",
            }
        ]
    diff_text = git(
        "diff",
        "--no-color",
        "-U0",
        base_ref,
        head_ref,
        "--",
        file_path,
        cwd=cwd,
    ).stdout
    findings = analyze_patch_diff(
        diff_text, base_lines, head_lines, manifest, file_path
    )
    if not findings:
        findings.append(
            {
                "file": file_path,
                "old_start": 0,
                "old_count": 0,
                "new_start": 0,
                "new_count": 0,
                "sections": [],
                "section": None,
                "protection": "review",
                "reason": "Patch file metadata changed without a textual hunk.",
                "header_changed": False,
                "sample": "",
            }
        )
    return findings


def commit_metadata(sha: str, cwd: str | Path) -> dict[str, Any]:
    return {
        "sha": sha,
        "subject": git_out("show", "-s", "--format=%s", sha, cwd=cwd),
        "body": git_out("show", "-s", "--format=%B", sha, cwd=cwd),
        "author": git_out("show", "-s", "--format=%an", sha, cwd=cwd),
        "date": git_out("show", "-s", "--format=%aI", sha, cwd=cwd),
        "committer_date": git_out("show", "-s", "--format=%cI", sha, cwd=cwd),
        "parents": git_out("show", "-s", "--format=%P", sha, cwd=cwd).split(),
    }


def classify_commit(
    sha: str, manifest: dict[str, Any], cwd: str | Path
) -> dict[str, Any]:
    meta = commit_metadata(sha, cwd)
    result: dict[str, Any] = {
        **meta,
        "decision": "review",
        "risk": "high",
        "protection": "review",
        "classes": [],
        "requires_build": False,
        "security": False,
        "files": [],
        "patch_findings": [],
        "reason": "",
    }

    if len(meta["parents"]) > 1:
        result["reason"] = "Merge commit; automatic integration is not attempted."
        result["classes"] = ["unknown"]
        return result
    if not meta["parents"]:
        result["reason"] = "Root commit; automatic integration is not attempted."
        result["classes"] = ["unknown"]
        return result

    changes = diff_name_status(meta["parents"][0], sha, cwd)
    rules: list[dict[str, Any]] = []
    for change in changes:
        inspected = {change["old_path"], change["path"]}
        candidates = [classify_path(path, manifest) for path in sorted(inspected)]
        worst = max(
            candidates,
            key=lambda rule: PROTECTION_RANK.get(rule["protection"], 1),
        )
        rules.append(worst)
        result["files"].append(
            {
                "status": change["status"],
                "path": change["path"],
                "old_path": change["old_path"],
                "class": worst["class"],
                "protection": worst["protection"],
                "requires_build": bool(worst.get("requires_build", False)),
                "reason": worst["reason"],
            }
        )
        if worst.get("requires_build"):
            result["requires_build"] = True

    patch_files = {path for path in manifest["patch_files"]}
    for change in changes:
        for path in {change["old_path"], change["path"]}:
            if path in patch_files:
                findings = analyze_patch_change(
                    meta["parents"][0], sha, path, manifest, cwd
                )
                result["patch_findings"].extend(findings)
                result["requires_build"] = True

    result["classes"] = sorted({rule["class"] for rule in rules})
    message = (meta["subject"] + "\n" + meta["body"]).lower()
    keywords = [str(word).lower() for word in manifest.get("security_keywords", [])]
    if any(keyword in message for keyword in keywords):
        result["security"] = True
        result["classes"] = sorted(set(result["classes"]) | {"security"})

    protected_paths = [rule for rule in rules if rule["protection"] == "protected"]
    review_paths = [rule for rule in rules if rule["protection"] == "review"]
    patch_blocking = [
        finding
        for finding in result["patch_findings"]
        if finding["protection"] in ("protected", "review")
    ]

    blocking_reasons: list[str] = []
    if protected_paths:
        blocking_reasons.append(
            "touches protected paths: "
            + ", ".join(sorted({rule["pattern"] for rule in protected_paths}))
        )
    if review_paths:
        blocking_reasons.append(
            "touches review-only paths: "
            + ", ".join(sorted({rule["pattern"] for rule in review_paths}))
        )
    if patch_blocking:
        blocking_reasons.append(
            "touches patch.sh sections: "
            + ", ".join(
                sorted(
                    {
                        (finding.get("section") or "unlabeled")
                        for finding in patch_blocking
                    }
                )
            )
        )

    if blocking_reasons:
        result["decision"] = "review"
        result["reason"] = "; ".join(blocking_reasons)
        if protected_paths or any(
            finding["protection"] == "protected" for finding in patch_blocking
        ):
            result["protection"] = "protected"
        else:
            result["protection"] = "review"
    else:
        result["decision"] = "candidate"
        result["protection"] = "candidate"
        result["reason"] = "All changed paths and patch hunks are candidate-eligible."

    if result["security"] or result["protection"] == "protected":
        result["risk"] = "high"
    elif "dependency" in result["classes"]:
        result["risk"] = "high"
    elif result["requires_build"]:
        result["risk"] = "medium"
    else:
        result["risk"] = "low"

    return result


# ---------------------------------------------------------------------------
# State and report helpers
# ---------------------------------------------------------------------------


def empty_state(upstream: str) -> dict[str, Any]:
    timestamp = now_utc()
    return {
        "schema_version": 1,
        "upstream": upstream,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_scanned_sha": None,
        "processed_shas": [],
        "integrated_shas": [],
        "pending": [],
        "deferred": [],
        "candidate": None,
        "history": [],
    }


def _normalize_sha_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("sha")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
            if value not in result:
                result.append(value)
    return result


def load_state(path: str | Path, upstream: str) -> dict[str, Any]:
    data = read_json(path, default=None)
    if not data:
        return empty_state(upstream)
    if not isinstance(data, dict):
        raise AutomationError(f"{path}: state must be a JSON object")
    state = empty_state(upstream)
    state.update({key: value for key, value in data.items() if key in state})
    state["upstream"] = data.get("upstream") or upstream
    state["processed_shas"] = _normalize_sha_list(data.get("processed_shas"))
    state["integrated_shas"] = _normalize_sha_list(data.get("integrated_shas"))
    state["pending"] = _normalize_pending(data.get("pending"))
    state["deferred"] = _normalize_pending(data.get("deferred"))
    candidate = data.get("candidate")
    if isinstance(candidate, dict):
        state["candidate"] = {
            "target_sha": candidate.get("target_sha"),
            "shas": _normalize_sha_list(candidate.get("shas")),
            "risk": candidate.get("risk", "high"),
            "requires_full_build": bool(candidate.get("requires_full_build", True)),
            "created_at": candidate.get("created_at"),
        }
    else:
        state["candidate"] = None
    if not isinstance(state["history"], list):
        state["history"] = []
    return state


def _normalize_pending(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str):
            if not re.fullmatch(r"[0-9a-f]{40}", value):
                continue
            entry = {"sha": value, "subject": "", "reason": "", "risk": "high"}
        elif isinstance(value, dict):
            sha = value.get("sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            entry = {
                "sha": sha,
                "subject": str(value.get("subject", "")),
                "reason": str(value.get("reason", "")),
                "risk": str(value.get("risk", "high")),
                "first_seen": value.get("first_seen"),
                "report": value.get("report"),
            }
        else:
            continue
        if entry["sha"] in seen:
            continue
        seen.add(entry["sha"])
        result.append(entry)
    return result


def state_sha_set(state: dict[str, Any], key: str) -> set[str]:
    return {entry["sha"] for entry in state.get(key, []) if isinstance(entry, dict)}


def save_state(path: str | Path, state: dict[str, Any], max_history: int = 100) -> None:
    state["updated_at"] = now_utc()
    history = state.get("history", [])
    if isinstance(history, list) and len(history) > max_history:
        state["history"] = history[-max_history:]
    write_json(path, state)


def risk_rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 2)


def overall_risk(commits: Iterable[dict[str, Any]]) -> str:
    risk = "low"
    for commit in commits:
        if risk_rank(commit.get("risk", "high")) > risk_rank(risk):
            risk = commit.get("risk", "high")
    return risk


def parse_pr_markers(body: str) -> dict[str, Any]:
    def shas(name: str) -> list[str]:
        pattern = re.compile(
            rf"<!--\s*{re.escape(name)}\s*:\s*([0-9a-f, ]*)\s*-->", re.IGNORECASE
        )
        match = pattern.search(body or "")
        if not match:
            return []
        values = [part.strip() for part in match.group(1).split(",")]
        return [
            value
            for value in values
            if re.fullmatch(r"[0-9a-f]{40}", value)
        ]

    explicit = re.findall(r"(?im)^\s*Upstream-SHA:\s*([0-9a-f]{40})\s*$", body or "")
    included = shas("upstream-sync:included")
    sync_pending = shas("upstream-sync:pending")
    review_pending = shas("upstream-review:pending")
    target_match = re.search(
        r"<!--\s*upstream-(?:sync|review):target\s*:\s*([0-9a-f]{40})\s*-->",
        body or "",
        re.IGNORECASE,
    )
    automerge = bool(
        re.search(
            r"<!--\s*upstream-sync:automerge\s*:\s*yes\s*-->",
            body or "",
            re.IGNORECASE,
        )
    )
    if re.search(r"<!--\s*upstream-sync:version\s*:", body or "", re.IGNORECASE):
        kind = "sync"
    elif re.search(
        r"<!--\s*upstream-review:version\s*:", body or "", re.IGNORECASE
    ):
        kind = "review"
    else:
        kind = None
    return {
        "kind": kind,
        "included": included,
        "sync_pending": sync_pending,
        "review_pending": review_pending,
        "explicit_integrated": explicit,
        "target": target_match.group(1) if target_match else None,
        "automerge": automerge,
    }


def sha_list(values: Iterable[str]) -> str:
    return ",".join(values)


def short_sha(sha: str | None) -> str:
    return (sha or "")[:12]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join([line, separator, *body])


def report_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Upstream sync report: `{short_sha(report.get('target_sha'))}`")
    lines.append("")
    lines.append(
        _table(
            ["Field", "Value"],
            [
                ["Result", f"`{report.get('result')}`"],
                ["Upstream", f"`{report.get('upstream')}`"],
                ["Base", f"`{short_sha(report.get('base_sha'))}`"],
                ["Target", f"`{report.get('target_sha')}`"],
                ["Included", str(len(report.get("included_shas", [])))],
                ["Pending review", str(len(report.get("pending_shas", [])))],
                ["Risk", f"`{report.get('risk')}`"],
                ["Full build required", str(report.get("requires_full_build"))],
                ["Generated", report.get("generated_at", "")],
            ],
        )
    )
    lines.append("")

    included = report.get("commits_included", [])
    if included:
        lines.append("## Included in the candidate branch")
        lines.append("")
        lines.append(
            _table(
                ["Commit", "Subject", "Risk", "Build"],
                [
                    [
                        f"`{short_sha(commit['sha'])}`",
                        commit.get("subject", ""),
                        commit.get("risk", ""),
                        "yes" if commit.get("requires_build") else "no",
                    ]
                    for commit in included
                ],
            )
        )
        lines.append("")

    pending = report.get("commits_pending", [])
    if pending:
        lines.append("## Requires human review")
        lines.append("")
        lines.append(
            _table(
                ["Commit", "Subject", "Reason"],
                [
                    [
                        f"`{short_sha(commit['sha'])}`",
                        commit.get("subject", ""),
                        commit.get("reason", ""),
                    ]
                    for commit in pending
                ],
            )
        )
        lines.append("")

    findings = [
        finding
        for commit in [*included, *pending]
        for finding in commit.get("patch_findings", [])
        if finding.get("protection") in ("protected", "review")
    ]
    if findings:
        lines.append("## patch.sh hunk classification")
        lines.append("")
        lines.append(
            _table(
                ["Commit", "Section", "Protection", "Lines", "Reason"],
                [
                    [
                        f"`{short_sha(finding.get('commit', ''))}`",
                        finding.get("section") or "unlabeled",
                        finding.get("protection", ""),
                        f"{finding.get('new_start')},{finding.get('new_count')}",
                        finding.get("reason", ""),
                    ]
                    for finding in findings[:40]
                ],
            )
        )
        if len(findings) > 40:
            lines.append("")
            lines.append(f"_{len(findings) - 40} additional findings are in the JSON report._")
        lines.append("")

    conflicts = report.get("conflicts", [])
    if conflicts:
        lines.append("## Cherry-pick conflicts")
        lines.append("")
        for conflict in conflicts:
            lines.append(
                f"- `{short_sha(conflict.get('sha'))}` {conflict.get('subject', '')}: "
                f"{conflict.get('reason', '')}"
            )
        lines.append("")

    lines.append("## Next steps")
    lines.append("")
    for step in report.get("next_steps", []):
        lines.append(f"- {step}")
    lines.append("")
    lines.append("_Generated by .github/scripts/sync_upstream.py. This report is an aid; "
                 "the source of truth is the Git diff and the protection manifest._")
    lines.append("")
    return "\n".join(lines)


def candidate_pr_body(report: dict[str, Any]) -> str:
    included = report.get("included_shas", [])
    pending = report.get("pending_shas", [])
    markers = [
        "<!-- upstream-sync:version: 1 -->",
        f"<!-- upstream-sync:target: {report.get('target_sha', '')} -->",
        f"<!-- upstream-sync:included: {sha_list(included)} -->",
        f"<!-- upstream-sync:pending: {sha_list(pending)} -->",
        "<!-- upstream-sync:automerge: "
        + ("yes" if report.get("automerge_eligible") else "no")
        + " -->",
    ]
    lines = ["\n".join(markers), "", report_markdown(report)]
    lines.append("")
    lines.append("## Merge requirements")
    lines.append("")
    lines.append("- [ ] Human review (auto-merge is off unless explicitly enabled).")
    if report.get("requires_full_build"):
        lines.append(
            "- [ ] Successful **exact-tree** full build (`Validate build` → mode `full`) "
            "recorded on the state branch."
        )
    else:
        lines.append("- [x] Full build is not required for this change set.")
    lines.append("- [ ] `ui-protection` and `lightweight-validation` checks are green.")
    if pending:
        lines.append(
            f"- [ ] The {len(pending)} commit(s) listed under *Requires human review* "
            "are handled separately; merging this PR does not integrate them."
        )
    lines.append("")
    return "\n".join(lines)


def review_pr_body(report: dict[str, Any]) -> str:
    pending = report.get("pending_shas", [])
    markers = [
        "<!-- upstream-review:version: 1 -->",
        f"<!-- upstream-review:target: {report.get('target_sha', '')} -->",
        f"<!-- upstream-review:pending: {sha_list(pending)} -->",
    ]
    lines = ["\n".join(markers), "", report_markdown(report)]
    lines.append("")
    lines.append("## What to do")
    lines.append("")
    lines.append(
        "This PR contains **only a review report**, not the conflicting upstream code. "
        "That is intentional: unresolved conflict state must never be pushed as if it "
        "were a valid commit."
    )
    lines.append("")
    lines.append(
        "1. Inspect the report and the affected upstream commits."
    )
    lines.append(
        "2. Apply the required changes manually on a dedicated branch, preserving custom UI."
    )
    lines.append(
        "3. If you integrated specific upstream commits, add `Upstream-SHA: <sha>` lines "
        "to this PR body so the merge recorder marks them integrated."
    )
    lines.append(
        "4. Alternatively, merge this report PR as an acknowledgement; its pending commits "
        "will be recorded as deferred and will not be retried until a manual retry."
    )
    lines.append("")
    return "\n".join(lines)


def review_file_markdown(report: dict[str, Any]) -> str:
    body = report_markdown(report).split("\n")
    if body and body[0].startswith("# "):
        body = body[1:]
        while body and not body[0].strip():
            body = body[1:]
    lines = [
        f"# Manual review required for upstream `{report.get('target_sha')}`",
        "",
        "\n".join(body).rstrip(),
        "",
    ]
    return "\n".join(lines)


def append_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown)
        if not markdown.endswith("\n"):
            handle.write("\n")


def emit_github_output(values: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
