from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SYNC = SCRIPTS / "sync_upstream.py"
RECORD = SCRIPTS / "record_merge.py"
BUILD_POLICY = SCRIPTS / "build_policy.py"

MANIFEST = {
    "schema_version": 1,
    "upstream": {"default_repo": "upstream/example", "default_branch": "main"},
    "policy": {
        "default_path_protection": "review",
        "default_patch_protection": "review",
        "auto_merge": False,
        "require_human_approval": True,
        "state_branch": "automation/state",
        "candidate_branch": "automation/upstream-candidate",
        "review_branch": "automation/upstream-review",
        "max_history_entries": 100,
        "max_commits_per_run": 200,
    },
    "path_rules": [
        {
            "pattern": "src/**",
            "class": "docs",
            "protection": "candidate",
            "requires_build": False,
            "reason": "safe source test path",
        },
        {
            "pattern": "README.md",
            "class": "docs",
            "protection": "candidate",
            "requires_build": False,
            "reason": "documentation",
        },
        {
            "pattern": "patch.sh",
            "class": "patch",
            "protection": "section",
            "requires_build": True,
            "reason": "patch surface",
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


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(*args: str, cwd: Path) -> str:
    return run(["git", *args], cwd).stdout.strip()


class SyncEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        git("init", "-b", "main", cwd=self.upstream)
        git("config", "user.name", "Upstream", cwd=self.upstream)
        git("config", "user.email", "upstream@example.com", cwd=self.upstream)

        (self.upstream / ".github").mkdir()
        (self.upstream / ".github" / "ui-protection.yml").write_text(
            json.dumps(MANIFEST, indent=2), encoding="utf-8"
        )
        (self.upstream / "patch.sh").write_text(
            "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|b|' Toolbar.java\n"
            "# ext: settings\nsed -i 's|c|d|' Settings.java\n",
            encoding="utf-8",
        )
        (self.upstream / "README.md").write_text("base readme\n", encoding="utf-8")
        (self.upstream / "src").mkdir()
        (self.upstream / "src" / "app.js").write_text(
            "const value = 1;\n", encoding="utf-8"
        )
        (self.upstream / "src" / "feature.js").write_text(
            "export const feature = 1;\n", encoding="utf-8"
        )
        git("add", ".", cwd=self.upstream)
        self._commit("base", 0)

        self.base = git("rev-parse", "HEAD", cwd=self.upstream)
        self.safe_sha = self._change(
            "feat: safe feature",
            {"src/feature.js": "export const feature = 2;\n"},
            1,
        )
        self.protected_sha = self._change(
            "feat: toolbar change",
            {
                "patch.sh": (
                    "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|protected|' Toolbar.java\n"
                    "# ext: settings\nsed -i 's|c|d|' Settings.java\n"
                )
            },
            2,
        )
        self.conflict_sha = self._change(
            "feat: conflicting app change",
            {"src/app.js": "const value = 3;\n"},
            3,
        )
        self.docs_sha = self._change(
            "docs: readme update", {"README.md": "base readme\nmore docs\n"}, 4
        )
        self.target = git("rev-parse", "HEAD", cwd=self.upstream)

        self.repo = self.root / "fork"
        run(["git", "clone", str(self.upstream), str(self.repo)], self.root)
        git("config", "user.name", "Fork", cwd=self.repo)
        git("config", "user.email", "fork@example.com", cwd=self.repo)
        git("reset", "--hard", self.base, cwd=self.repo)
        (self.repo / "src" / "app.js").write_text(
            "const value = 2;\n", encoding="utf-8"
        )
        git("add", "src/app.js", cwd=self.repo)
        git(
            "-c",
            "user.name=Fork",
            "-c",
            "user.email=fork@example.com",
            "commit",
            "-m",
            "local: keep Titanium app behavior",
            cwd=self.repo,
        )
        self.local_base = git("rev-parse", "HEAD", cwd=self.repo)
        self.state_file = self.root / "state" / "state.json"
        self.reports = self.root / "reports"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _commit(self, message: str, offset: int) -> None:
        date = f"2026-01-01T00:{offset:02d}:00+00:00"
        env = {
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
            "GIT_AUTHOR_NAME": "Upstream",
            "GIT_AUTHOR_EMAIL": "upstream@example.com",
            "GIT_COMMITTER_NAME": "Upstream",
            "GIT_COMMITTER_EMAIL": "upstream@example.com",
        }
        merged = dict(os.environ)
        merged.update(env)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.upstream,
            env=merged,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

    def _change(self, message: str, changes: dict[str, str], offset: int) -> str:
        for path, content in changes.items():
            target = self.upstream / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git("add", ".", cwd=self.upstream)
        self._commit(message, offset)
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def sync(self) -> subprocess.CompletedProcess[str]:
        return run(
            [
                sys.executable,
                str(SYNC),
                "--repo",
                str(self.repo),
                "--upstream",
                str(self.upstream),
                "--upstream-ref",
                "refs/heads/main",
                "--base",
                "HEAD",
                "--state-file",
                str(self.state_file),
                "--report-dir",
                str(self.reports),
                "--upstream-name",
                "upstream/example",
            ],
            self.repo,
            check=False,
        )

    def test_candidate_is_partial_conflict_safe_and_deterministic(self) -> None:
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)

        # The conflicting cherry-pick was aborted; no conflict state remains.
        self.assertFalse((self.repo / ".git" / "CHERRY_PICK_HEAD").exists())
        self.assertEqual(git("status", "--porcelain", cwd=self.repo), "")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "HEAD")

        candidate = "automation/upstream-candidate"
        review = "automation/upstream-review"
        self.assertTrue(
            run(["git", "rev-parse", "--verify", candidate], self.repo, check=False)
            .returncode
            == 0
        )
        self.assertTrue(
            run(["git", "rev-parse", "--verify", review], self.repo, check=False)
            .returncode
            == 0
        )

        candidate_files = git(
            "diff", "--name-only", f"{self.local_base}..{candidate}", cwd=self.repo
        ).splitlines()
        self.assertEqual(sorted(candidate_files), ["README.md", "src/feature.js"])

        review_files = git(
            "diff", "--name-only", f"{self.local_base}..{review}", cwd=self.repo
        ).splitlines()
        self.assertEqual(len(review_files), 1)
        self.assertTrue(review_files[0].startswith(".github/upstream-sync/reviews/"))

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["last_scanned_sha"], self.target)
        self.assertEqual(
            set(state["candidate"]["shas"]), {self.safe_sha, self.docs_sha}
        )
        self.assertEqual(
            {entry["sha"] for entry in state["pending"]},
            {self.protected_sha, self.conflict_sha},
        )
        self.assertEqual(state["candidate"]["risk"], "low")
        self.assertFalse(state["candidate"]["requires_full_build"])
        self.assertIn(self.safe_sha, state["processed_shas"])
        self.assertIn(self.protected_sha, state["processed_shas"])

        candidate_body = (self.reports / "candidate-pr.md").read_text(encoding="utf-8")
        self.assertIn(self.safe_sha, candidate_body)
        self.assertIn(self.docs_sha, candidate_body)
        self.assertIn(self.protected_sha, candidate_body)
        review_body = (self.reports / "review-pr.md").read_text(encoding="utf-8")
        self.assertIn(self.protected_sha, review_body)
        self.assertIn(self.conflict_sha, review_body)
        report = json.loads((self.reports / "change-report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["automerge_eligible"])
        self.assertIn(self.conflict_sha, report["pending_shas"])

        first_candidate_sha = git("rev-parse", candidate, cwd=self.repo)

        again = self.sync()
        self.assertEqual(again.returncode, 0, again.stderr)
        second_candidate_sha = git("rev-parse", candidate, cwd=self.repo)
        self.assertEqual(first_candidate_sha, second_candidate_sha)
        state_again = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            {entry["sha"] for entry in state_again["pending"]},
            {self.protected_sha, self.conflict_sha},
        )
        self.assertEqual(len(state_again["history"]), 2)

    def test_candidate_state_is_cleared_when_commits_are_already_present(self) -> None:
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            set(state["candidate"]["shas"]), {self.safe_sha, self.docs_sha}
        )

        # The maintainer lands the candidate content on main directly, so every
        # carried candidate commit becomes an empty cherry-pick on the next run.
        git("switch", "main", cwd=self.repo)
        git("cherry-pick", self.safe_sha, self.docs_sha, cwd=self.repo)
        self.assertEqual(
            (self.repo / "src" / "feature.js").read_text(encoding="utf-8"),
            "export const feature = 2;\n",
        )
        self.assertIn(
            "more docs", (self.repo / "README.md").read_text(encoding="utf-8")
        )

        again = self.sync()
        self.assertEqual(again.returncode, 0, again.stderr)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIsNone(state["candidate"])
        self.assertIn(self.safe_sha, state["integrated_shas"])
        self.assertIn(self.docs_sha, state["integrated_shas"])
        report = json.loads(
            (self.reports / "change-report.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["candidate_state_cleared"])
        self.assertFalse(report["candidate_created"])

    def test_record_merge_marks_only_included_shas(self) -> None:
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = (self.reports / "candidate-pr.md").read_text(encoding="utf-8")
        merge_sha = "e" * 40
        event = {
            "pull_request": {
                "number": 17,
                "body": body,
                "merged": True,
                "merge_commit_sha": merge_sha,
                "title": "chore(upstream): candidate",
                "html_url": "https://example.invalid/pull/17",
                "user": {"login": "automation"},
                "merged_at": "2026-01-01T01:00:00Z",
                "head": {"ref": "automation/upstream-candidate"},
                "base": {"ref": "main"},
            }
        }
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        record_report = self.root / "merge-report"
        record = run(
            [
                sys.executable,
                str(RECORD),
                "--state-dir",
                str(self.state_file.parent),
                "--report-dir",
                str(record_report),
                "--event",
                str(event_path),
                "--manifest",
                str(self.repo / ".github" / "ui-protection.yml"),
                "--merged",
                "--merge-commit",
                merge_sha,
            ],
            self.repo,
            check=False,
        )
        self.assertEqual(record.returncode, 0, record.stderr)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn(self.safe_sha, state["integrated_shas"])
        self.assertIn(self.docs_sha, state["integrated_shas"])
        self.assertNotIn(self.protected_sha, state["integrated_shas"])
        self.assertIsNone(state["candidate"])
        self.assertTrue((self.state_file.parent / "merges" / "pr-17.json").is_file())

        history_len = len(state["history"])
        again = run(
            [
                sys.executable,
                str(RECORD),
                "--state-dir",
                str(self.state_file.parent),
                "--report-dir",
                str(record_report),
                "--event",
                str(event_path),
                "--manifest",
                str(self.repo / ".github" / "ui-protection.yml"),
                "--merged",
                "--merge-commit",
                merge_sha,
            ],
            self.repo,
            check=False,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        state_again = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(state_again["history"]), history_len)
        self.assertEqual(
            state_again["integrated_shas"].count(self.safe_sha), 1
        )

    def test_build_policy_requires_build_for_patch_and_not_for_candidate(self) -> None:
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.root / "policy-candidate"
        policy = run(
            [
                sys.executable,
                str(BUILD_POLICY),
                "--repo",
                str(self.repo),
                "--base",
                self.local_base,
                "--head",
                "automation/upstream-candidate",
                "--report-dir",
                str(out),
            ],
            self.repo,
            check=False,
        )
        self.assertEqual(policy.returncode, 0, policy.stderr)
        data = json.loads((out / "build-policy.json").read_text(encoding="utf-8"))
        self.assertFalse(data["full_build_required"])
        self.assertEqual(data["risk"], "low")

        out2 = self.root / "policy-protected"
        policy2 = run(
            [
                sys.executable,
                str(BUILD_POLICY),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                self.target,
                "--report-dir",
                str(out2),
            ],
            self.repo,
            check=False,
        )
        self.assertEqual(policy2.returncode, 0, policy2.stderr)
        data2 = json.loads((out2 / "build-policy.json").read_text(encoding="utf-8"))
        self.assertTrue(data2["full_build_required"])
        self.assertIn("patch.sh", " ".join(data2["reasons"]))

    def test_build_policy_ignores_default_branch_advancement(self) -> None:
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)

        git("switch", "main", cwd=self.repo)
        (self.repo / "patch.sh").write_text(
            "#!/bin/bash\n# ext: toolbar\nsed -i 's|a|advanced|' Toolbar.java\n"
            "# ext: settings\nsed -i 's|c|d|' Settings.java\n",
            encoding="utf-8",
        )
        git("add", "patch.sh", cwd=self.repo)
        git("commit", "-m", "local: protected follow-up", cwd=self.repo)
        advanced = git("rev-parse", "HEAD", cwd=self.repo)
        git("switch", "--detach", self.local_base, cwd=self.repo)

        out = self.root / "policy-advanced-base"
        policy = run(
            [
                sys.executable,
                str(BUILD_POLICY),
                "--repo",
                str(self.repo),
                "--base",
                advanced,
                "--head",
                "automation/upstream-candidate",
                "--report-dir",
                str(out),
            ],
            self.repo,
            check=False,
        )
        self.assertEqual(policy.returncode, 0, policy.stderr)
        data = json.loads((out / "build-policy.json").read_text(encoding="utf-8"))
        self.assertFalse(data["full_build_required"])
        self.assertEqual(data["risk"], "low")
        self.assertEqual(data["diff_base_sha"], self.local_base)

    def test_no_upstream_common_ancestor_is_refused(self) -> None:
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        git("init", "-b", "main", cwd=unrelated)
        git("config", "user.name", "Unrelated", cwd=unrelated)
        git("config", "user.email", "unrelated@example.com", cwd=unrelated)
        (unrelated / "other.txt").write_text("x\n", encoding="utf-8")
        git("add", "other.txt", cwd=unrelated)
        git("commit", "-m", "unrelated", cwd=unrelated)
        result = run(
            [
                sys.executable,
                str(SYNC),
                "--repo",
                str(self.repo),
                "--upstream",
                str(unrelated),
                "--upstream-ref",
                "refs/heads/main",
                "--base",
                "HEAD",
                "--state-file",
                str(self.root / "state-unrelated.json"),
                "--report-dir",
                str(self.root / "unrelated-report"),
            ],
            self.repo,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no common ancestor", result.stderr)
        self.assertFalse((self.root / "state-unrelated.json").exists())


if __name__ == "__main__":
    unittest.main()
