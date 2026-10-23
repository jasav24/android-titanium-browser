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
        self.apk.write_bytes(b"release apk bytes")
        self.marker_path = self.root / "marker.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_marker(self, files: list[Path] | None = None, **overrides: object) -> None:
        files = files if files is not None else [self.apk]
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
                    "name": path.name,
                    "file": path.name,
                    "size": path.stat().st_size,
                    "sha256": digest(path.read_bytes()),
                }
                for path in files
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
        extra = self.artifacts / "extra-arm64-v8a.apk"
        extra.write_bytes(b"unrecorded apk")
        self.write_marker()
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("absent from the marker", result.stderr)

    def test_different_run_is_refused(self) -> None:
        self.write_marker(run={"id": "999", "repository": "owner/repo"})
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("different workflow run", result.stderr)

    def test_aab_is_refused(self) -> None:
        aab = self.artifacts / "titanium-arm64-v8a.aab"
        aab.write_bytes(b"release aab bytes")
        self.write_marker(files=[self.apk, aab])
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("APK-only", result.stderr)

    def test_armv7_apk_is_refused(self) -> None:
        self.apk.unlink()
        armv7 = self.artifacts / "titanium-armeabi-v7a.apk"
        armv7.write_bytes(b"armv7 apk")
        self.write_marker(files=[armv7])
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an arm64-v8a build", result.stderr)

    def test_recorded_metadata_must_match_downloaded_bytes(self) -> None:
        report = self.artifacts / "smoke-report.md"
        report.write_text("Status: **passed**\n", encoding="utf-8")
        self.write_marker(files=[self.apk, report])
        report.write_text("Status: **failed**\n", encoding="utf-8")
        result = self.run_verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 mismatch for 'smoke-report.md'", result.stderr)


if __name__ == "__main__":
    unittest.main()
