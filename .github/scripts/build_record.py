#!/usr/bin/env python3
"""Record or query exact-tree full-build markers on the automation state branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from automation_lib import AutomationError, write_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--lookup-tree", help="print the marker for this tree")
    parser.add_argument("--tree")
    parser.add_argument("--ref")
    parser.add_argument("--result", choices=["success", "failure"])
    parser.add_argument("--kind", choices=["test", "release"], default="test")
    parser.add_argument("--signing", choices=["ephemeral", "release"], default="ephemeral")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument(
        "--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    )
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-url", default="")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="artifact name and local file path; repeat for each artifact",
    )
    return parser.parse_args(argv)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_tree(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value or ""):
        raise AutomationError(f"{label} must be a 40-character lowercase Git SHA")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir = Path(args.state_dir)
    builds_dir = state_dir / "builds"

    if args.lookup_tree:
        tree = assert_tree(args.lookup_tree, "--lookup-tree")
        marker = builds_dir / f"{tree}.json"
        if not marker.exists():
            print(f"no build marker for tree {tree}")
            return 1
        data = json.loads(marker.read_text(encoding="utf-8"))
        print(json.dumps(data, indent=2))
        return 0 if data.get("result") == "success" else 1

    if not args.tree or not args.result:
        raise AutomationError("--tree and --result are required to record a build")
    tree = assert_tree(args.tree, "--tree")
    commit = assert_tree(args.ref, "--ref") if args.ref else None

    artifacts = []
    for value in args.artifact:
        if "=" not in value:
            raise AutomationError(f"--artifact must be NAME=PATH, got {value!r}")
        name, raw_path = value.split("=", 1)
        path = Path(raw_path)
        if not path.is_file():
            raise AutomationError(f"artifact does not exist: {path}")
        artifacts.append(
            {
                "name": name,
                "file": path.name,
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    if args.result == "success" and not artifacts:
        raise AutomationError(
            "a successful build must record at least one artifact; "
            "refusing to create a marker with no downloadable output"
        )

    record = {
        "schema_version": 1,
        "tree": tree,
        "commit": commit,
        "result": args.result,
        "kind": args.kind,
        "signing": args.signing,
        "recorded_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "run": {
            "id": args.run_id,
            "attempt": args.run_attempt,
            "url": args.run_url,
            "repository": args.repository,
        },
        "artifacts": artifacts,
    }

    builds_dir.mkdir(parents=True, exist_ok=True)
    if args.result == "success":
        marker = builds_dir / f"{tree}.json"
        if args.kind == "test" and marker.exists():
            try:
                existing = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
            if existing.get("result") == "success" and existing.get("kind") == "release":
                print(
                    f"tree {tree} already has a release marker; "
                    "refusing to replace it with a test-build marker"
                )
                return 0
        write_json(marker, record)
        lines = [
            f"# Full build success for tree `{tree}`",
            "",
            f"- Commit: `{commit or 'unknown'}`",
            f"- Kind: `{args.kind}`",
            f"- Signing: `{args.signing}`",
            f"- Run: [{args.run_id}.{args.run_attempt}]({args.run_url})"
            if args.run_url
            else f"- Run: `{args.run_id}.{args.run_attempt}`",
            "",
            "## Artifacts",
            "",
            "| Name | File | Size | SHA-256 |",
            "| --- | --- | --- | --- |",
        ]
        for artifact in artifacts:
            lines.append(
                f"| `{artifact['name']}` | `{artifact['file']}` | "
                f"{artifact['size']} | `{artifact['sha256']}` |"
            )
        lines.append("")
        (builds_dir / f"{tree}.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"recorded successful build marker for {tree} ({len(artifacts)} artifacts)")
    else:
        failures = builds_dir / "failures"
        failures.mkdir(parents=True, exist_ok=True)
        path = failures / f"{tree}-{args.run_id}-{args.run_attempt}.json"
        write_json(path, record)
        print(f"recorded failed build attempt for {tree}: {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
