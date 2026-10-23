from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
BUILD_RECORD = SCRIPTS / "build_record.py"
TREE = "1" * 40
COMMIT = "2" * 40


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILD_RECORD), *args],
        text=True,
        capture_output=True,
        check=False,
    )


class BuildRecordTests(unittest.TestCase):
    def test_success_records_artifact_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            artifact = root / "app.apk"
            artifact.write_bytes(b"test apk")
            result = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--ref",
                COMMIT,
                "--result",
                "success",
                "--kind",
                "test",
                "--signing",
                "ephemeral",
                "--run-id",
                "123",
                "--artifact",
                f"app.apk={artifact}",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = state / "builds" / f"{TREE}.json"
            self.assertTrue(marker.is_file())
            data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(data["tree"], TREE)
            self.assertEqual(data["commit"], COMMIT)
            self.assertEqual(data["result"], "success")
            self.assertEqual(len(data["artifacts"]), 1)
            self.assertEqual(data["artifacts"][0]["name"], "app.apk")
            self.assertEqual(len(data["artifacts"][0]["sha256"]), 64)

            lookup = run_script("--state-dir", str(state), "--lookup-tree", TREE)
            self.assertEqual(lookup.returncode, 0, lookup.stderr)
            self.assertIn(TREE, lookup.stdout)

    def test_failure_does_not_create_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            result = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--ref",
                COMMIT,
                "--result",
                "failure",
                "--run-id",
                "9",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((state / "builds" / f"{TREE}.json").exists())
            failures = list((state / "builds" / "failures").glob("*.json"))
            self.assertEqual(len(failures), 1)
            data = json.loads(failures[0].read_text(encoding="utf-8"))
            self.assertEqual(data["result"], "failure")
            lookup = run_script("--state-dir", str(state), "--lookup-tree", TREE)
            self.assertEqual(lookup.returncode, 1)

    def test_success_without_artifacts_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            result = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--result",
                "success",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("must record at least one artifact", result.stderr)
            self.assertFalse((state / "builds" / f"{TREE}.json").exists())

    def test_test_build_does_not_downgrade_release_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            artifact = root / "app.apk"
            artifact.write_bytes(b"release apk")
            release = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--ref",
                COMMIT,
                "--result",
                "success",
                "--kind",
                "release",
                "--signing",
                "release",
                "--run-id",
                "1",
                "--artifact",
                f"app.apk={artifact}",
            )
            self.assertEqual(release.returncode, 0, release.stderr)
            test = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--ref",
                COMMIT,
                "--result",
                "success",
                "--kind",
                "test",
                "--signing",
                "ephemeral",
                "--run-id",
                "2",
                "--artifact",
                f"app.apk={artifact}",
            )
            self.assertEqual(test.returncode, 0, test.stderr)
            self.assertIn("refusing to replace", test.stdout)
            data = json.loads(
                (state / "builds" / f"{TREE}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(data["kind"], "release")
            self.assertEqual(data["signing"], "release")
            self.assertEqual(data["run"]["id"], "1")

    def test_missing_artifact_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            result = run_script(
                "--state-dir",
                str(state),
                "--tree",
                TREE,
                "--result",
                "success",
                "--artifact",
                "missing.apk=/does/not/exist.apk",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("artifact does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
