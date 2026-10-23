#!/usr/bin/env python3
"""Expose ui-protection.yml policy values to workflow steps as outputs."""

from __future__ import annotations

import argparse
import json

from automation_lib import emit_github_output, load_manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    policy = manifest["policy"]
    build = manifest.get("build", {})
    info = {
        "upstream_repo": manifest["upstream"]["default_repo"],
        "upstream_branch": manifest["upstream"]["default_branch"],
        "state_branch": policy["state_branch"],
        "candidate_branch": policy["candidate_branch"],
        "review_branch": policy["review_branch"],
        "runner_labels": json.dumps(build.get("runner_labels", [])),
        "artifact_retention_days": int(build.get("artifact_retention_days", 14)),
        "min_free_disk_gib": build.get("min_free_disk_gib", 100),
        "auto_merge": str(bool(policy.get("auto_merge", False))).lower(),
    }
    emit_github_output(info)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
