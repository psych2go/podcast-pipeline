import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import agent_pipeline as ap
from scripts.content_finalizer import ContentFinalizationError


class NarrationRepairTests(unittest.TestCase):
    def test_notes_internal_wording_triggers_targeted_rewrite_then_retry(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text("## 一\n正文\n", encoding="utf-8")
            notes = folder / "中文完整笔记.md"
            notes.write_text("第一行\n这是小鼠实验，公开稿应限定为小鼠。\n", encoding="utf-8")
            calls = {"n": 0}

            def fake_finalize(_folder):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ContentFinalizationError(
                        "完整笔记包含面向内部的审查过程语言: 第2行")
                return {"ok": True}

            def fake_edit(_folder, prompt, **kwargs):
                self.assertIn("公开稿应", prompt)
                self.assertEqual(kwargs["allowed_files"], [notes])
                notes.write_text("第一行\n这一结果仅限小鼠实验。\n", encoding="utf-8")

            with patch.object(ap, "finalize_content_package", side_effect=fake_finalize), \
                    patch.object(ap, "run_edit_task", side_effect=fake_edit) as edit:
                self.assertEqual(ap._finalize_with_depth_repair(folder), {"ok": True})
            self.assertEqual(edit.call_count, 1)

    def test_narration_repair_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text("## 一\n公开稿应保留。\n", encoding="utf-8")
            (folder / "中文完整笔记.md").write_text("x\n", encoding="utf-8")
            err = ContentFinalizationError("讲稿包含面向内部的审查过程语言: 第2行")
            with patch.object(ap, "finalize_content_package", side_effect=err), \
                    patch.object(ap, "run_edit_task") as edit:
                with self.assertRaises(ContentFinalizationError):
                    ap._finalize_with_depth_repair(folder)
            self.assertEqual(edit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
