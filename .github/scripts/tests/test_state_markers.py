from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from automation_lib import (  # noqa: E402
    candidate_pr_body,
    empty_state,
    load_state,
    parse_pr_markers,
    review_pr_body,
    save_state,
)

A = "a" * 40
B = "b" * 40
C = "c" * 40


class StateTests(unittest.TestCase):
    def test_empty_state_shape(self) -> None:
        state = empty_state("upstream/example")
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["upstream"], "upstream/example")
        for key in (
            "last_scanned_sha",
            "processed_shas",
            "integrated_shas",
            "pending",
            "deferred",
            "candidate",
            "history",
        ):
            self.assertIn(key, state)

    def test_load_state_normalizes_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "upstream": "upstream/example",
                        "processed_shas": [A, A, "not-a-sha", {"sha": B}],
                        "integrated_shas": [C, None],
                        "pending": [
                            {"sha": A, "subject": "one", "reason": "protected"},
                            {"sha": A, "subject": "duplicate"},
                            "bad",
                        ],
                        "deferred": [{"sha": B}],
                        "candidate": {
                            "target_sha": C,
                            "shas": [A, B, B],
                            "risk": "low",
                            "requires_full_build": 0,
                        },
                        "history": [{"result": "sync"}],
                    }
                ),
                encoding="utf-8",
            )
            state = load_state(path, "fallback")
            self.assertEqual(state["processed_shas"], [A, B])
            self.assertEqual(state["integrated_shas"], [C])
            self.assertEqual(len(state["pending"]), 1)
            self.assertEqual(state["pending"][0]["sha"], A)
            self.assertEqual(state["deferred"][0]["sha"], B)
            self.assertEqual(state["candidate"]["shas"], [A, B])
            self.assertFalse(state["candidate"]["requires_full_build"])
            self.assertEqual(len(state["history"]), 1)

    def test_invalid_candidate_becomes_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({"candidate": "bad"}), encoding="utf-8")
            state = load_state(path, "upstream/example")
            self.assertIsNone(state["candidate"])

    def test_save_state_truncates_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            state = empty_state("upstream/example")
            state["history"] = [{"n": index} for index in range(6)]
            save_state(path, state, max_history=3)
            loaded = load_state(path, "upstream/example")
            self.assertEqual([item["n"] for item in loaded["history"]], [3, 4, 5])


class MarkerTests(unittest.TestCase):
    def test_candidate_body_round_trips(self) -> None:
        report = {
            "target_sha": C,
            "included_shas": [A, B],
            "pending_shas": [],
            "result": "changes",
            "upstream": "upstream/example",
            "base_sha": B,
            "risk": "low",
            "requires_full_build": False,
            "automerge_eligible": True,
            "generated_at": "2026-01-01T00:00:00Z",
            "commits_included": [],
            "commits_pending": [],
            "conflicts": [],
            "next_steps": [],
        }
        body = candidate_pr_body(report)
        markers = parse_pr_markers(body)
        self.assertEqual(markers["kind"], "sync")
        self.assertEqual(markers["target"], C)
        self.assertEqual(markers["included"], [A, B])
        self.assertEqual(markers["sync_pending"], [])
        self.assertTrue(markers["automerge"])
        self.assertEqual(markers["explicit_integrated"], [])

    def test_candidate_body_marks_automerge_no(self) -> None:
        report = {
            "target_sha": C,
            "included_shas": [A],
            "pending_shas": [B],
            "result": "changes",
            "upstream": "upstream/example",
            "base_sha": B,
            "risk": "low",
            "requires_full_build": False,
            "automerge_eligible": False,
            "generated_at": "2026-01-01T00:00:00Z",
            "commits_included": [],
            "commits_pending": [{"sha": B, "subject": "x", "reason": "protected"}],
            "conflicts": [],
            "next_steps": [],
        }
        markers = parse_pr_markers(candidate_pr_body(report))
        self.assertFalse(markers["automerge"])
        self.assertEqual(markers["sync_pending"], [B])

    def test_review_body_and_explicit_integration(self) -> None:
        report = {
            "target_sha": C,
            "included_shas": [],
            "pending_shas": [A, B],
            "result": "changes",
            "upstream": "upstream/example",
            "base_sha": B,
            "risk": "high",
            "requires_full_build": True,
            "automerge_eligible": False,
            "generated_at": "2026-01-01T00:00:00Z",
            "commits_included": [],
            "commits_pending": [
                {"sha": A, "subject": "one", "reason": "protected"},
                {"sha": B, "subject": "two", "reason": "conflict"},
            ],
            "conflicts": [],
            "next_steps": [],
        }
        body = review_pr_body(report)
        body += f"\nUpstream-SHA: {A}\n"
        markers = parse_pr_markers(body)
        self.assertEqual(markers["kind"], "review")
        self.assertEqual(markers["target"], C)
        self.assertEqual(markers["review_pending"], [A, B])
        self.assertEqual(markers["explicit_integrated"], [A])

    def test_unmanaged_body_has_no_kind(self) -> None:
        markers = parse_pr_markers("An ordinary pull request.")
        self.assertIsNone(markers["kind"])
        self.assertEqual(markers["included"], [])


if __name__ == "__main__":
    unittest.main()
