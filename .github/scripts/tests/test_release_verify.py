from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
VERIFY = SCRIPTS / "verify_release_artifacts.py"
TREE = "1" * 40
COMMIT = "2" * 40
RUN_ID = "12345"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.apk = self.artifacts / "titanium-arm64-v8a.apk"
        self.aab = self.artifacts / "titanium-arm64-v8a.aab"
        self.apk.write_bytes(b"release apk bytes")
        self.aab.write_bytes(b"release aab bytes")
        self.marker_path = self.root / "marker.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_marker(self, **overrides: object) -> None:
        marker = {
            "schema_version": 1,
            "tree": TREE,
            "commit": COMMIT,
            "result": "success",
            "kind": "release",
            "signing": "release",
            "run": {"id": RUN_ID, "repository": "owner/repo"},
            "artifacts": [
                {
                    "name": self.apk.name,
                    "file": self.apk.name,
                    "size": self.apk.stat().st_size,
                    "sha256": digest(self.apk.read_bytes()),
                },
                {
                    "name": self.aab.name,
                    "file": self.aab.name,
                    "size": self.aab.stat().st_size,
                    "sha256": digest(self.aab.read_bytes()),
                },
            ],
        }
        marker.update(overrides)
        self.marker_path.write_text(json.dumps(marker), encoding="utf-8")

    def run_verify(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(VERIFY),
                "--marker",
                str(self.marker_path),
                "--artifacts",
                str(self.artifacts),
                "--tree",
                TREE,
                "--ref",
                COMMIT,
                "--run-id",
                RUN_ID,
                "--repository",
                "owner/repo",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_matching_assets_and_marker_pass(self) -> None:
        self.write_marker()
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verification passed", result.stdout)

    def test_same_size_tamper_is_detected(self) -> None:
        self.write_marker()
        self.apk.write_bytes(b"release apk bytez")
        self.assertEqual(self.apk.stat().st_size, len(b"release apk bytes"))
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 mismatch", result.stderr)

    def test_test_build_marker_is_refused(self) -> None:
        self.write_marker(kind="test", signing="ephemeral")
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not recorded from a release build", result.stderr)

    def test_asset_missing_from_marker_is_refused(self) -> None:
        self.write_marker()
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        marker["artifacts"] = marker["artifacts"][:1]
        self.marker_path.write_text(json.dumps(marker), encoding="utf-8")
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("absent from the marker", result.stderr)

    def test_different_run_is_refused(self) -> None:
        self.write_marker(run={"id": "999", "repository": "owner/repo"})
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("different workflow run", result.stderr)


if __name__ == "__main__":
    unittest.main()
