from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from automation_lib import (  # noqa: E402
    SECTION_HEADER_RE,
    classify_path,
    load_manifest,
    path_matches,
)

ROOT = Path(__file__).resolve().parents[3]


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(ROOT / ".github/ui-protection.yml")

    def test_policy_is_conservative(self) -> None:
        policy = self.manifest["policy"]
        self.assertFalse(policy["auto_merge"])
        self.assertTrue(policy["require_human_approval"])
        self.assertEqual(policy["state_branch"], "automation/state")
        self.assertEqual(policy["candidate_branch"], "automation/upstream-candidate")

    def test_patch_sections_match_patch_sh(self) -> None:
        headers = []
        for line in (ROOT / "patch.sh").read_text(encoding="utf-8").splitlines():
            match = SECTION_HEADER_RE.match(line.rstrip())
            if match:
                headers.append(match.group(1).strip())
        declared = [entry["header"] for entry in self.manifest["patch_sections"]]
        self.assertEqual(len(declared), 23)
        self.assertEqual(set(headers), set(declared))
        self.assertEqual(len(set(declared)), len(declared))

    def test_protected_paths(self) -> None:
        for path in (
            "res/drawable/themed_app_icon.xml",
            "fastlane/metadata/android/en-US/images/phoneScreenshots/1.png",
            "extensions/dist/manifest.json",
            "args.gn",
            ".github/ui-protection.yml",
        ):
            rule = classify_path(path, self.manifest)
            self.assertEqual(rule["protection"], "protected", path)

    def test_manifest_self_protection_wins_over_generic_ci_rule(self) -> None:
        specific = classify_path(".github/ui-protection.yml", self.manifest)
        generic = classify_path(".github/workflows/release.yml", self.manifest)
        self.assertEqual(specific["protection"], "protected")
        self.assertEqual(generic["protection"], "review")

    def test_review_paths(self) -> None:
        for path in (
            ".github/workflows/release.yml",
            ".github/scripts/sync_upstream.py",
            "build.sh",
            "common.sh",
            ".gitmodules",
            "LICENSE",
        ):
            rule = classify_path(path, self.manifest)
            self.assertEqual(rule["protection"], "review", path)

    def test_candidate_paths(self) -> None:
        for path in ("README.md", ".gitignore", "vanadium", ".gclient"):
            rule = classify_path(path, self.manifest)
            self.assertEqual(rule["protection"], "candidate", path)
            if path in ("vanadium", ".gclient"):
                self.assertTrue(rule.get("requires_build", False), path)
            elif path == ".gitignore":
                self.assertFalse(rule.get("requires_build", False), path)

    def test_patch_path_is_section_classified(self) -> None:
        rule = classify_path("patch.sh", self.manifest)
        self.assertEqual(rule["protection"], "section")
        self.assertTrue(rule["requires_build"])

    def test_unknown_paths_default_to_review_with_build(self) -> None:
        rule = classify_path("src/some/new/File.java", self.manifest)
        self.assertEqual(rule["protection"], "review")
        self.assertTrue(rule["requires_build"])
        self.assertEqual(rule["class"], "unknown")

    def test_path_matching(self) -> None:
        self.assertTrue(path_matches("res", "res/**"))
        self.assertTrue(path_matches("res/drawable/icon.png", "res/**"))
        self.assertFalse(path_matches("resources/icon.png", "res/**"))
        self.assertTrue(path_matches("res\\drawable\\icon.png", "res/**"))
        self.assertTrue(path_matches(".github/workflows/x.yml", ".github/**"))
        self.assertFalse(path_matches(".github/workflows/x.yml", ".github"))

    def test_every_protected_rule_has_a_reason(self) -> None:
        for rule in self.manifest["path_rules"]:
            self.assertTrue(rule["reason"].strip(), rule["pattern"])
        for section in self.manifest["patch_sections"]:
            self.assertTrue(section["reason"].strip(), section["header"])


if __name__ == "__main__":
    unittest.main()
