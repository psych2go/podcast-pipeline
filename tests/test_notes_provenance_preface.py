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


class NotesProvenanceSectionTest(unittest.TestCase):
    def test_drops_leading_evidence_basis_section(self):
        text = (
            "# 中文完整笔记\n\n## 证据基础\n\n"
            "本笔记依据原始转录.txt，内容按 content_map.json 的 included 单元整理。\n\n"
            "## 领导力的多种路径\n\n正文。\n"
        )
        cleaned = strip_notes_provenance_preface(text)
        self.assertNotIn("证据基础", cleaned)
        self.assertIn("## 领导力的多种路径\n\n正文。", cleaned)
        self.assertEqual(audit_narration_issues(cleaned), [])

    def test_keeps_evidence_section_with_public_prose(self):
        text = "# 笔记\n\n## 证据基础\n\n这是一段公开内容。\n\n## 章\n正文\n"
        self.assertEqual(strip_notes_provenance_preface(text), text)
