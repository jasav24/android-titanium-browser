#!/usr/bin/env python3
"""Verify release assets against the exact-tree marker on the state branch.

The publish job downloads the same artifact bundle that the record job wrote to
`builds/<tree>.json`. Checking the downloaded bytes against that marker makes a
later artifact replacement fail closed even when both files agree with a
rewritten SHA256SUMS file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from automation_lib import AutomationError, append_step_summary, write_json

SHA256_RE = re.compile(r"[0-9a-f]{64}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", required=True, help="download/builds/<tree>.json")
    parser.add_argument("--artifacts", required=True, help="directory of release assets")
    parser.add_argument("--tree", required=True)
    parser.add_argument("--ref", required=True, help="release commit SHA")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--json-out")
    return parser.parse_args(argv)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(problems: list[str], message: str) -> None:
    problems.append(message)


def verify(
    marker: dict[str, Any],
    artifacts_dir: Path,
    tree: str,
    commit: str,
    run_id: str,
    repository: str,
) -> list[str]:
    problems: list[str] = []

    if marker.get("schema_version") != 1:
        fail(problems, "marker schema_version is not 1")
    if marker.get("result") != "success":
        fail(problems, "marker does not describe a successful build")
    if marker.get("kind") != "release":
        fail(problems, "marker was not recorded from a release build")
    if marker.get("signing") != "release":
        fail(problems, "marker was not signed with the release key")
    if marker.get("tree") != tree:
        fail(problems, "marker tree does not match the release commit tree")
    if marker.get("commit") != commit:
        fail(problems, "marker commit does not match the release commit")
    run = marker.get("run") or {}
    if str(run.get("id", "")) != str(run_id):
        fail(problems, "marker was recorded by a different workflow run")
    if repository and run.get("repository") not in ("", repository):
        fail(problems, "marker was recorded by a different repository")

    entries = marker.get("artifacts")
    if not isinstance(entries, list) or not entries:
        fail(problems, "marker has no recorded artifacts")
        entries = []

    by_name: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            fail(problems, f"artifact entry {index} is not an object")
            continue
        name = entry.get("file") or entry.get("name")
        if not isinstance(name, str) or not name or name != Path(name).name:
            fail(problems, f"artifact entry {index} has an unsafe file name")
            continue
        if name in by_name:
            fail(problems, f"artifact {name!r} is recorded more than once")
            continue
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            fail(problems, f"artifact {name!r} has an invalid size")
        if not isinstance(entry.get("sha256"), str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            fail(problems, f"artifact {name!r} has an invalid SHA-256")
        by_name[name] = entry

    actual_apks = sorted(
        path
        for path in artifacts_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".apk"
    )
    actual_aabs = sorted(
        path
        for path in artifacts_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".aab"
    )
    if actual_aabs:
        fail(
            problems,
            "an AAB is present, but release publishing is APK-only: "
            + ", ".join(path.name for path in actual_aabs),
        )
    for path in actual_apks:
        if "arm64-v8a" not in path.name:
            fail(
                problems,
                f"downloaded APK {path.name!r} is not an arm64-v8a build",
            )
    if len(actual_apks) != 1:
        fail(
            problems,
            f"expected exactly one ARM64 APK for publication, found {len(actual_apks)}",
        )

    for path in actual_apks + actual_aabs:
        if path.name not in by_name:
            fail(problems, f"downloaded artifact {path.name!r} is absent from the marker")

    for name, entry in sorted(by_name.items()):
        path = artifacts_dir / name
        if not path.is_file():
            fail(problems, f"recorded artifact {name!r} was not downloaded")
            continue
        if path.stat().st_size != entry["size"]:
            fail(problems, f"size mismatch for {name!r}")
        if sha256(path) != entry["sha256"]:
            fail(problems, f"SHA-256 mismatch for {name!r}")

    return problems


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    marker_path = Path(args.marker)
    artifacts_dir = Path(args.artifacts)
    if not marker_path.is_file():
        raise AutomationError(f"build marker does not exist: {marker_path}")
    if not artifacts_dir.is_dir():
        raise AutomationError(f"artifact directory does not exist: {artifacts_dir}")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutomationError(f"invalid build marker JSON: {exc}") from exc
    if not isinstance(marker, dict):
        raise AutomationError("build marker must be a JSON object")

    problems = verify(
        marker,
        artifacts_dir,
        args.tree,
        args.ref,
        args.run_id,
        args.repository,
    )
    report = {
        "tree": args.tree,
        "commit": args.ref,
        "run_id": args.run_id,
        "artifacts": sorted(
            path.name for path in artifacts_dir.iterdir() if path.is_file()
        ),
        "problems": problems,
        "verified": not problems,
    }
    if args.json_out:
        write_json(args.json_out, report)

    if problems:
        summary = ["# Release marker verification failed", ""]
        summary.extend(f"- {problem}" for problem in problems)
        append_step_summary("\n".join(summary) + "\n")
        print("\n".join(problems), file=sys.stderr)
        return 1

    summary = (
        "# Release marker verification passed\n\n"
        f"Downloaded release assets match `builds/{args.tree}.json` "
        f"for run `{args.run_id}`.\n"
    )
    append_step_summary(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
