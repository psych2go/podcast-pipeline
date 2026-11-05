import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import agent_pipeline as ap
from scripts.content_finalizer import ContentFinalizationError

ERR = "无法安全重排 summary_map：需要唯一且匹配的章节标题"


def _setup(folder):
    (folder / "讲书稿.md").write_text("## 甲\n正文\n\n## 乙\n正文\n", encoding="utf-8")
    (folder / "summary_map.json").write_text(json.dumps(
        {"chapters": [{"title": "甲", "unit_ids": ["u1"], "claim_ids": []}]},
        ensure_ascii=False), encoding="utf-8")


class SummaryMapRepairTests(unittest.TestCase):
    def test_title_mismatch_triggers_summary_only_edit_then_retry(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _setup(folder)
            calls = {"n": 0}

            def fake_finalize(_folder):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ContentFinalizationError(ERR)
                return {"ok": True}

            def fake_edit(_folder, prompt, **kwargs):
                self.assertIn("乙", prompt)
                self.assertEqual(kwargs["allowed_files"], [folder / "summary_map.json"])
                self.assertEqual(kwargs["task_name"], "content_summary_map_repair")

            with patch.object(ap, "finalize_content_package", side_effect=fake_finalize), \
                    patch.object(ap, "run_edit_task", side_effect=fake_edit) as edit:
                self.assertEqual(ap._finalize_with_depth_repair(folder), {"ok": True})
            self.assertEqual(edit.call_count, 1)

    def test_summary_map_repair_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _setup(folder)
            with patch.object(ap, "finalize_content_package",
                              side_effect=ContentFinalizationError(ERR)), \
                    patch.object(ap, "run_edit_task") as edit:
                with self.assertRaises(ContentFinalizationError):
                    ap._finalize_with_depth_repair(folder)
            self.assertEqual(edit.call_count, 2)

    def test_request_lists_missing_chapter(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _setup(folder)
            req = ap.build_summary_map_repair_request(folder)
            self.assertIn("缺少的章节：['乙']", req["prompt"])


if __name__ == "__main__":
    unittest.main()
