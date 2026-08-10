import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import subagent


class SubagentIsolationTests(unittest.TestCase):
    def test_legacy_call_only_stages_files_named_in_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            named_input = folder / "input.txt"
            unrelated = folder / "secret.txt"
            output = folder / "output.md"
            named_input.write_text("source evidence", encoding="utf-8")
            unrelated.write_text("must not be staged", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                self.assertTrue((workspace / "input.txt").exists())
                self.assertFalse((workspace / "secret.txt").exists())
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Read input.txt and write output.md.",
                    task_name="legacy_compatibility",
                    allowed_files=[output],
                )

            self.assertEqual(result["input_files"], ["input.txt"])
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "generated content",
            )

    def test_allowed_output_is_committed_from_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                self.assertNotEqual(workspace, folder)
                self.assertEqual(
                    (workspace / "input.txt").read_text(encoding="utf-8"),
                    "source evidence",
                )
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Read input.txt and write output.md.",
                    task_name="isolation_success",
                    input_files=[source],
                    allowed_files=[output],
                    required_files=[output],
                )

            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "generated content",
            )
            self.assertEqual(result["committed_files"], ["output.md"])
            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")

    def test_unauthorized_new_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                (workspace / "rogue.txt").write_text(
                    "unauthorized", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                with self.assertRaisesRegex(
                        subagent.SubagentError, "未允许的文件"):
                    subagent.run_edit_task(
                        folder,
                        "Read input.txt and write output.md.",
                        task_name="isolation_rogue",
                        input_files=[source],
                        allowed_files=[output],
                        required_files=[output],
                    )

            self.assertFalse(output.exists())
            self.assertFalse((folder / "rogue.txt").exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")

    def test_input_tampering_is_rejected_without_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")
            output.write_text("previous output", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                (workspace / "input.txt").write_text(
                    "tampered evidence", encoding="utf-8")
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                with self.assertRaisesRegex(
                        subagent.SubagentError, "input_files"):
                    subagent.run_edit_task(
                        folder,
                        "Read input.txt and update output.md.",
                        task_name="isolation_tamper",
                        input_files=[source],
                        allowed_files=[output],
                        required_files=[output],
                    )

            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output")

    def test_remove_missing_outputs_does_not_seed_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            output = folder / "optional.txt"
            output.write_text("stale", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                self.assertFalse((Path(workspace) / "optional.txt").exists())
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Delete optional.txt when no replacement is needed.",
                    task_name="remove_stale_output",
                    allowed_files=[output],
                    remove_missing_outputs=True,
                )

            self.assertFalse(output.exists())
            self.assertEqual(result["removed_files"], ["optional.txt"])


if __name__ == "__main__":
    unittest.main()
