import unittest

from scripts.content_finalizer import strip_notes_provenance_preface
from scripts.validator import audit_narration_issues


class NotesProvenancePrefaceTest(unittest.TestCase):
    def test_strips_preface_before_first_chapter(self):
        text = (
            "# 中文完整笔记\n\n"
            "转录依据：原始转录.txt。本笔记以 content_map.json 为完整性台账。\n\n"
            "## U0001 开场\n正文。\n"
        )
        cleaned = strip_notes_provenance_preface(text)
        self.assertNotIn("转录依据", cleaned)
        self.assertIn("## U0001 开场\n正文。", cleaned)
        self.assertNotIn("\n\n\n", cleaned)
        self.assertEqual(audit_narration_issues(cleaned), [])

    def test_keeps_matching_line_inside_chapter(self):
        text = "# 笔记\n\n## 章\n转录依据：章节里的引用保持不变。\n"
        self.assertEqual(strip_notes_provenance_preface(text), text)


if __name__ == "__main__":
    unittest.main()
