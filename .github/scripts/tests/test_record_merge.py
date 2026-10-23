from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
RECORD = SCRIPTS / "record_merge.py"

A = "a" * 40
B = "b" * 40
TARGET = "c" * 40
MERGE = "d" * 40


def marker_block(included: list[str]) -> str:
    return "\n".join(
        [
            "<!-- upstream-sync:version: 1 -->",
            f"<!-- upstream-sync:target: {TARGET} -->",
            f"<!-- upstream-sync:included: {', '.join(included)} -->",
            "<!-- upstream-sync:pending:  -->",
            "<!-- upstream-sync:automerge: no -->",
        ]
    )


class RecordMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.state_path = self.state_dir / "state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "upstream": "upstream/example",
                    "candidate": {
                        "target_sha": TARGET,
                        "shas": [A, B],
                        "risk": "low",
                        "requires_full_build": False,
                    },
                    "integrated_shas": [],
                    "pending": [],
                    "deferred": [],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, included: list[str]) -> subprocess.CompletedProcess[str]:
        event = self.root / "event.json"
        event.write_text(
            json.dumps(
                {
                    "pull_request": {
                        "number": 7,
                        "merged": True,
                        "merged_at": "2026-01-01T00:00:00Z",
                        "merge_commit_sha": MERGE,
                        "body": marker_block(included),
                        "title": "candidate",
                        "html_url": "https://example.invalid/pull/7",
                        "user": {"login": "maintainer"},
                        "head": {"ref": "automation/upstream-candidate"},
                        "base": {"ref": "main"},
                    }
                }
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                str(RECORD),
                "--state-dir",
                str(self.state_dir),
                "--report-dir",
                str(self.root / "report"),
                "--event",
                str(event),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_partial_merge_keeps_unmerged_candidate_commits(self) -> None:
        result = self.record([A])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["integrated_shas"], [A])
        self.assertEqual(state["candidate"]["shas"], [B])

    def test_full_merge_clears_the_candidate(self) -> None:
        result = self.record([A, B])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["integrated_shas"]), [A, B])
        self.assertIsNone(state["candidate"])


if __name__ == "__main__":
    unittest.main()
