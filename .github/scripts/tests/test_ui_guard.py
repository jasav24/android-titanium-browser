from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
UI_GUARD = SCRIPTS / "ui_guard.py"

MANIFEST = {
    "schema_version": 1,
    "upstream": {"default_repo": "upstream/example", "default_branch": "main"},
    "policy": {
        "default_path_protection": "review",
        "default_patch_protection": "review",
        "auto_merge": False,
        "state_branch": "automation/state",
        "candidate_branch": "automation/upstream-candidate",
        "review_branch": "automation/upstream-review",
    },
    "path_rules": [
        {
            "pattern": "res/**",
            "class": "protected",
            "protection": "protected",
            "requires_build": True,
            "reason": "branding",
        },
        {
            "pattern": "patch.sh",
            "class": "patch",
            "protection": "section",
            "requires_build": True,
            "reason": "patch surface",
        },
        {
            "pattern": "build.sh",
            "class": "build",
            "protection": "review",
            "requires_build": True,
            "reason": "build pipeline",
        },
        {
            "pattern": "src/**",
            "class": "docs",
            "protection": "candidate",
            "requires_build": False,
            "reason": "safe source",
        },
        {
            "pattern": "**",
            "class": "unknown",
            "protection": "review",
            "requires_build": True,
            "reason": "default deny",
        },
    ],
    "patch_files": ["patch.sh"],
    "patch_sections": [
        {"header": "ext: toolbar", "protection": "protected", "reason": "toolbar"},
        {"header": "ext: settings", "protection": "review", "reason": "settings"},
    ],
    "security_keywords": ["cve"],
}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


class UiGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "user.email", "test@example.com", cwd=self.repo)
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "ui-protection.yml").write_text(
            json.dumps(MANIFEST, indent=2), encoding="utf-8"
        )
        (self.repo / "patch.sh").write_text(
            "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|b|' Toolbar.java\n"
            "# ext: settings\nsed -i 's|c|d|' Settings.java\n",
            encoding="utf-8",
        )
        (self.repo / "res").mkdir()
        (self.repo / "res" / "icon.xml").write_text("<icon/>\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "file.js").write_text("const a = 1;\n", encoding="utf-8")
        (self.repo / "build.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "base", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_head(self, branch: str, changes: dict[str, str]) -> str:
        git("switch", "-c", branch, self.base, cwd=self.repo)
        for path, content in changes.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", branch, cwd=self.repo)
        head = git("rev-parse", "HEAD", cwd=self.repo)
        git("switch", "--detach", self.base, cwd=self.repo)
        return head

    def run_guard(
        self, head: str, *extra: str, base: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        report = Path(self.temp.name) / "report"
        return subprocess.run(
            [
                sys.executable,
                str(UI_GUARD),
                "--repo",
                str(self.repo),
                "--base",
                base or self.base,
                "--head",
                head,
                "--report-dir",
                str(report),
                *extra,
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def report(self) -> dict:
        return json.loads(
            (Path(self.temp.name) / "report" / "ui-protection.json").read_text(
                encoding="utf-8"
            )
        )

    def test_safe_change_passes(self) -> None:
        head = self.make_head("safe", {"src/file.js": "const a = 2;\n"})
        result = self.run_guard(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["decision"], "pass")

    def test_review_only_change_requires_review(self) -> None:
        head = self.make_head("review", {"build.sh": "#!/bin/bash\necho hi\n"})
        result = self.run_guard(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["decision"], "review-required")
        self.assertEqual(self.report()["counts"]["review"], 1)

    def test_protected_patch_and_asset_change_blocks(self) -> None:
        head = self.make_head(
            "protected",
            {
                "patch.sh": (
                    "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|changed|' Toolbar.java\n"
                    "# ext: settings\nsed -i 's|c|d|' Settings.java\n"
                ),
                "res/icon.xml": "<icon color='red'/>\n",
            },
        )
        result = self.run_guard(head)
        self.assertEqual(result.returncode, 1)
        report = self.report()
        self.assertEqual(report["decision"], "blocked")
        self.assertGreaterEqual(report["counts"]["protected"], 2)
        kinds = {finding["kind"] for finding in report["findings"]}
        self.assertIn("path", kinds)
        self.assertIn("patch-hunk", kinds)
        self.assertIn("res/icon.xml", result.stdout)

    def test_acknowledgement_turns_blocked_into_acknowledged(self) -> None:
        head = self.make_head(
            "acknowledged",
            {
                "patch.sh": (
                    "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|ack|' Toolbar.java\n"
                    "# ext: settings\nsed -i 's|c|d|' Settings.java\n"
                )
            },
        )
        result = self.run_guard(head, "--acknowledged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["decision"], "acknowledged")
        self.assertTrue(self.report()["acknowledged"])

    def test_review_section_hunk_is_review_not_protected(self) -> None:
        head = self.make_head(
            "review-section",
            {
                "patch.sh": (
                    "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|b|' Toolbar.java\n"
                    "# ext: settings\nsed -i 's|c|changed|' Settings.java\n"
                )
            },
        )
        result = self.run_guard(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["decision"], "review-required")

    def test_base_advanced_after_head_does_not_blame_the_pull_request(self) -> None:
        head = self.make_head(
            "behind-default", {"src/file.js": "const a = 2;\n"}
        )
        git("switch", "main", cwd=self.repo)
        (self.repo / "res" / "icon.xml").write_text(
            "<icon color='red'/>\n", encoding="utf-8"
        )
        git("add", "res/icon.xml", cwd=self.repo)
        git("commit", "-m", "protected base follow-up", cwd=self.repo)
        advanced = git("rev-parse", "HEAD", cwd=self.repo)
        git("switch", "--detach", self.base, cwd=self.repo)

        result = self.run_guard(head, base=advanced)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
