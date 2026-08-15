import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_review
import catalog
import content_map
import episode
import html_gen
import validator


class CatalogModuleSplitTests(unittest.TestCase):
    def test_catalog_facade_keeps_implementation_in_deep_modules(self):
        self.assertLess(len(Path(catalog.__file__).read_text().splitlines()), 450)
        for name in ("catalog_core.py", "catalog_site.py", "catalog_publish.py"):
            self.assertTrue((ROOT / "scripts" / name).exists())


class CssConsolidationTests(unittest.TestCase):
    def test_html_template_has_no_duplicate_selector_or_media_layer(self):
        import tinycss2
        source = Path(html_gen.__file__).read_text(encoding="utf-8")
        css = source.split("<style>", 1)[1].split("</style>", 1)[0]
        rules = tinycss2.parse_stylesheet(
            css, skip_whitespace=True, skip_comments=True)
        selectors = [
            tinycss2.serialize(rule.prelude).strip()
            for rule in rules if rule.type == "qualified-rule"
        ]
        media = [
            tinycss2.serialize(rule.prelude).strip()
            for rule in rules
            if rule.type == "at-rule" and rule.lower_at_keyword == "media"
        ]
        self.assertEqual(len(selectors), len(set(selectors)))
        self.assertEqual(len(media), len(set(media)))
        self.assertFalse([rule for rule in rules if rule.type == "error"])


class CanonicalSectionReaderTests(unittest.TestCase):
    def test_validator_and_content_map_share_canonical_chapter_bodies(self):
        text = "导览。\n\n## 第一章\n正文一。\n补充。\n\n## 第二章\n正文二。"
        preamble, blocks = validator._chapter_blocks(text)
        self.assertEqual(preamble, "导览。")
        self.assertEqual(
            content_map.briefing_chapters(text),
            {block["title"]: block["body"] for block in blocks},
        )


class EvidenceV2SunsetTests(unittest.TestCase):
    def test_legacy_write_is_frozen_and_read_deadline_is_explicit(self):
        self.assertEqual(
            episode.LEGACY_EVIDENCE_WRITE_CUTOFF, date(2026, 8, 15))
        self.assertEqual(
            episode.LEGACY_EVIDENCE_READ_CUTOFF, date(2026, 9, 1))
        self.assertTrue(episode.legacy_evidence_read_allowed(date(2026, 8, 31)))
        self.assertFalse(episode.legacy_evidence_read_allowed(date(2026, 9, 1)))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "只读"):
                episode.set_claim_evidence_mode(Path(td), "legacy_broad")


class AiReviewIsolationTests(unittest.TestCase):
    def _episode(self, folder):
        for name in ai_review.REVIEW_FILES:
            (folder / name).write_text(f"current {name}", encoding="utf-8")
        (folder / "ai_review.json").write_text(json.dumps({
            "passed": True,
            "transcript_quality": {"score": 100},
            "reviewed_files": ai_review.reviewed_hashes(folder),
        }), encoding="utf-8")

    def test_review_staging_hides_previous_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            observed = {}

            def fake_runner(workspace, prompt, *_args, **_kwargs):
                observed["has_previous_review"] = (workspace / "ai_review.json").exists()
                observed["prompt"] = prompt
                return {"payload": {"passed": True}, "command": "fake"}

            with patch.object(ai_review, "run_json_task", side_effect=fake_runner), \
                    patch.object(ai_review, "update_cache_from_review", return_value=0):
                review = ai_review.run_ai_review(folder, persist=False)
            self.assertTrue(review["input_snapshot_verified"])
            self.assertFalse(observed["has_previous_review"])
            self.assertIn("不会提供上次 ai_review.json", observed["prompt"])
            self.assertNotIn("transcript_quality\": {\"score\": 100", observed["prompt"])

    def test_review_result_is_discarded_if_input_changes_during_run(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)

            def fake_runner(_workspace, _prompt, *_args, **_kwargs):
                (folder / "讲书稿.md").write_text("concurrent edit", encoding="utf-8")
                return {"payload": {"passed": True}, "command": "fake"}

            with patch.object(ai_review, "run_json_task", side_effect=fake_runner), \
                    patch.object(ai_review, "update_cache_from_review", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "已丢弃结果"):
                    ai_review.run_ai_review(folder, persist=False)


if __name__ == "__main__":
    unittest.main()
