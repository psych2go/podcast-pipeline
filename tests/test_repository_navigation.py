import importlib.util
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_public_checker():
    path = ROOT / "scripts" / "check_public_repo.py"
    spec = importlib.util.spec_from_file_location("check_public_repo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RepositoryNavigationTests(unittest.TestCase):
    def test_active_entry_docs_route_to_canonical_commands(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        for text in (readme, agents, claude):
            self.assertIn("scripts/process.py", text)
            self.assertIn("scripts/catalog.py finish", text)
            self.assertIn("finish-batch", text)
            self.assertIn("check_public_repo.py", text)

    def test_legacy_and_temporary_documents_are_absent(self):
        removed_paths = (
            ROOT / "improvement-plan.md",
            ROOT / "docs" / "archive",
            ROOT / "docs" / "README.md",
            ROOT / "scripts" / "legacy",
            ROOT / "scripts" / "README.md",
            ROOT / "scripts" / "流水线文档.md",
            ROOT / "scripts" / "提示词.txt",
            ROOT / "scripts" / "rerun_1_5.sh",
        )
        for path in removed_paths:
            self.assertFalse(path.exists(), f"obsolete path still exists: {path}")

        self.assertTrue((ROOT / "docs" / "pipeline.md").exists())
        self.assertTrue((ROOT / "benchmarks" / "reports" / "asr-model-policy.md").exists())
        self.assertTrue((ROOT / "benchmarks" / "reports" / "multispeaker-sources.md").exists())

    def test_active_docs_do_not_advertise_removed_commands_or_paths(self):
        active_docs = (
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "CLAUDE.md",
            ROOT / "docs" / "pipeline.md",
            ROOT / "scripts" / "纠错提示词.md",
            ROOT / "scripts" / "讲稿提示词.md",
        )
        forbidden = (
            "scripts/batch.py",
            "finish-all",
            "--target r2",
            "scripts/legacy",
            "docs/archive",
            "scripts/提示词.txt",
            "scripts/rerun_1_5.sh",
            "scripts/流水线文档.md",
        )
        for path in active_docs:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path} advertises obsolete token: {token}")

    def test_gitignore_covers_private_workspace(self):
        private_samples = (
            "content/example/原始转录.txt",
            "content/example/讲书稿.md",
            "site/index.html",
            "site/example/content.html",
            "reports/local.json",
            ".runlogs/example.log",
            "episode.mp3",
        )
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(private_samples) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = set(result.stdout.splitlines())
        self.assertEqual(ignored, set(private_samples))

        public_site_files = ("site/deploy.sh", "site/wrangler.toml")
        result = subprocess.run(
            ["git", "check-ignore", *public_site_files],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_public_checker_classifies_private_artifacts(self):
        checker = load_public_checker()
        cases = {
            "content/show/讲书稿.md": "private local directory",
            "site/index.html": "generated site output",
            "reports/run.json": "private local directory",
            ".env": "local secret/tool state",
            "sample.mp3": "source/generated media",
            "exports/转录_纠错.txt": "podcast content artifact",
        }
        for path, expected in cases.items():
            self.assertEqual(checker.privacy_reason(path), expected)

        self.assertIsNone(checker.privacy_reason("scripts/process.py"))
        self.assertIsNone(checker.privacy_reason("site/deploy.sh"))
        self.assertIsNone(checker.privacy_reason("benchmarks/asr-policy.json"))
        self.assertEqual(
            checker.text_privacy_reason("find /" + "home" + "/alice/private/audio.mp3"),
            "machine-local absolute path",
        )
        self.assertIsNone(checker.text_privacy_reason("content/example/audio.mp3"))


if __name__ == "__main__":
    unittest.main()
