from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from automation_lib import (  # noqa: E402
    analyze_patch_diff,
    load_manifest,
    parse_patch_hunks,
    section_at,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / ".github/ui-protection.yml"


def hunk(old_start: int, old_count: int, new_start: int, new_count: int, body: list[str]) -> str:
    lines = [
        "diff --git a/patch.sh b/patch.sh",
        "--- a/patch.sh",
        "+++ b/patch.sh",
        f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
    ]
    lines.extend(body)
    return "\n".join(lines) + "\n"


class PatchClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(MANIFEST)

    def test_protected_section_inherits_protection(self) -> None:
        base = ["#!/bin/bash", "# ext: toolbar", "sed -i 's|old|new|' Toolbar.java"]
        head = ["#!/bin/bash", "# ext: toolbar", "sed -i 's|old|changed|' Toolbar.java"]
        diff = hunk(
            2,
            2,
            2,
            2,
            [" # ext: toolbar", "-sed -i 's|old|new|' Toolbar.java", "+sed -i 's|old|changed|' Toolbar.java"],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["section"], "ext: toolbar")
        self.assertEqual(findings[0]["protection"], "protected")

    def test_brand_section_inherits_protection(self) -> None:
        base = ["#!/bin/bash", "# brand: display name", "sed -i 's|old|new|' Strings.grd"]
        head = ["#!/bin/bash", "# brand: display name", "sed -i 's|old|changed|' Strings.grd"]
        diff = hunk(
            2,
            2,
            2,
            2,
            [
                " # brand: display name",
                "-sed -i 's|old|new|' Strings.grd",
                "+sed -i 's|old|changed|' Strings.grd",
            ],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["section"], "brand: display name")
        self.assertEqual(findings[0]["protection"], "protected")

    def test_links_section_inherits_protection(self) -> None:
        base = ["#!/bin/bash", "# links: github in browser", "sed -i 's|old|new|' Delegate.java"]
        head = ["#!/bin/bash", "# links: github in browser", "sed -i 's|old|changed|' Delegate.java"]
        diff = hunk(
            2,
            2,
            2,
            2,
            [
                " # links: github in browser",
                "-sed -i 's|old|new|' Delegate.java",
                "+sed -i 's|old|changed|' Delegate.java",
            ],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["section"], "links: github in browser")
        self.assertEqual(findings[0]["protection"], "protected")

    def test_review_section_is_review(self) -> None:
        base = ["# accessibility", "sed -i 's|a|b|' A.java"]
        head = ["# accessibility", "sed -i 's|a|c|' A.java"]
        diff = hunk(
            1,
            2,
            1,
            2,
            [" # accessibility", "-sed -i 's|a|b|' A.java", "+sed -i 's|a|c|' A.java"],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["section"], "accessibility")
        self.assertEqual(findings[0]["protection"], "review")

    def test_preamble_is_unlabeled_and_review(self) -> None:
        base = ["#!/bin/bash", "sed -i 's|a|b|' App.java"]
        head = ["#!/bin/bash", "sed -i 's|a|c|' App.java"]
        diff = hunk(
            1,
            2,
            1,
            2,
            [" #!/bin/bash", "-sed -i 's|a|b|' App.java", "+sed -i 's|a|c|' App.java"],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertIsNone(findings[0]["section"])
        self.assertEqual(findings[0]["protection"], "review")

    def test_header_change_is_protected(self) -> None:
        base = ["# ext: toolbar", "sed -i 's|a|b|' A.java"]
        head = ["# ext: toolbar renamed", "sed -i 's|a|b|' A.java"]
        diff = hunk(
            1,
            1,
            1,
            1,
            ["-# ext: toolbar", "+# ext: toolbar renamed"],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertTrue(findings[0]["header_changed"])
        self.assertEqual(findings[0]["protection"], "protected")

    def test_multi_section_hunk_uses_worst_protection(self) -> None:
        base = ["# ext: settings", "sed a", "# ext: toolbar", "sed b"]
        head = ["# ext: settings", "sed c", "# ext: toolbar", "sed d"]
        diff = hunk(
            1,
            4,
            1,
            4,
            [
                " # ext: settings",
                "-sed a",
                "+sed c",
                " # ext: toolbar",
                "-sed b",
                "+sed d",
            ],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["protection"], "protected")
        self.assertIn("spans multiple sections", findings[0]["reason"])
        self.assertEqual(findings[0]["sections"], ["ext: settings", "ext: toolbar"])

    def test_unlisted_section_is_review(self) -> None:
        base = ["# ext: not-a-real-section", "sed a"]
        head = ["# ext: not-a-real-section", "sed b"]
        diff = hunk(
            1,
            2,
            1,
            2,
            [" # ext: not-a-real-section", "-sed a", "+sed b"],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["protection"], "review")

    def test_crbug_comment_after_desktop_menu_inherits_protected(self) -> None:
        base = [
            "# desktop: menu",
            "sed a",
            "",
            "# crbug.com/445475304: incognito back",
            "sed b",
        ]
        head = [
            "# desktop: menu",
            "sed a",
            "",
            "# crbug.com/445475304: incognito back",
            "sed c",
        ]
        diff = hunk(
            1,
            5,
            1,
            5,
            [
                " # desktop: menu",
                " sed a",
                " ",
                " # crbug.com/445475304: incognito back",
                "-sed b",
                "+sed c",
            ],
        )
        findings = analyze_patch_diff(diff, base, head, self.manifest, "patch.sh")
        self.assertEqual(findings[0]["section"], "desktop: menu")
        self.assertEqual(findings[0]["protection"], "protected")

    def test_parse_multiple_hunks(self) -> None:
        text = (
            "@@ -1,2 +1,2 @@\n"
            " old\n"
            "-old\n"
            "+new\n"
            "@@ -10,1 +10,2 @@\n"
            " context\n"
            "+added\n"
        )
        hunks = parse_patch_hunks(text)
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0]["new_start"], 1)
        self.assertEqual(hunks[1]["new_start"], 10)
        self.assertEqual(hunks[1]["new_count"], 2)

    def test_section_at_finds_nearest_previous_header(self) -> None:
        lines = ["#!/bin/bash", "# ext: toolbar", "sed a", "# crbug: not a section", "sed b"]
        self.assertIsNone(section_at(lines, 0))
        self.assertEqual(section_at(lines, 1), "ext: toolbar")
        self.assertEqual(section_at(lines, 2), "ext: toolbar")
        self.assertEqual(section_at(lines, 4), "ext: toolbar")


if __name__ == "__main__":
    unittest.main()
