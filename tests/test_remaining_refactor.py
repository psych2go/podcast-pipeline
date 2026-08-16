import json
import subprocess
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
import catalog_core
import catalog_publish
import content_map
import episode
import html_gen
import validator


class CatalogModuleSplitTests(unittest.TestCase):
    def test_catalog_facade_keeps_implementation_in_deep_modules(self):
        self.assertLess(len(Path(catalog.__file__).read_text().splitlines()), 450)
        for name in ("catalog_core.py", "catalog_site.py", "catalog_publish.py"):
            self.assertTrue((ROOT / "scripts" / name).exists())

    def test_direct_modules_have_explicit_runtime_configuration(self):
        old_paths = catalog_core.CatalogPaths(
            catalog_core.BASE_DIR, catalog_core.CONTENT_DIR,
            catalog_core.SITE_DIR, catalog_core.CATALOG)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = catalog_core.CatalogPaths(
                root, root / "content", root / "site",
                root / "content" / "播客目录.md")
            paths.content_dir.mkdir()
            paths.site_dir.mkdir()
            try:
                catalog_core.configure_paths(paths)
                self.assertEqual(catalog_core.rebuild_catalog(), [])
                catalog_publish.configure_paths(paths)
                self.assertTrue(
                    catalog_publish._candidate_catalog_errors("missing"))
            finally:
                catalog_core.configure_paths(old_paths)
                catalog_publish.configure_paths(old_paths)

    def test_package_mode_executes_publish_preflight_imports(self):
        code = r'''
import tempfile
from pathlib import Path
import scripts.catalog as catalog
import scripts.catalog_publish as publish
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    folder = root / "content" / "Episode"
    folder.mkdir(parents=True)
    catalog.BASE_DIR = root
    catalog.CONTENT_DIR = root / "content"
    catalog.SITE_DIR = root / "site"
    catalog.CATALOG = root / "content" / "播客目录.md"
    publish.build_quality_report = lambda _folder: {
        "passed": False, "errors": ["blocked"],
    }
    assert catalog._publish_preflight("Episode") is False
'''
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


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
            if name == "episode.json":
                text = json.dumps({
                    "schema_version": 1,
                    "display_title": "Episode",
                    "source": {"url": "https://example.com"},
                    "quality": {"content_review_status": "pending"},
                })
            elif name == "来源.md":
                text = "# 来源信息\n"
            else:
                text = f"current {name}"
            (folder / name).write_text(text, encoding="utf-8")
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

    def test_review_context_change_discards_result(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            (folder / ai_review.CACHE_FILENAME).write_text(
                '{"schema_version": 3, "entries": {}}', encoding="utf-8")

            def fake_runner(_workspace, _prompt, *_args, **_kwargs):
                (folder / ai_review.CACHE_FILENAME).write_text(
                    '{"schema_version": 3, "entries": {"changed": {}}}',
                    encoding="utf-8")
                return {"payload": {"passed": True}, "command": "fake"}

            with patch.object(ai_review, "run_json_task", side_effect=fake_runner), \
                    patch.object(ai_review, "update_cache_from_review") as cache:
                with self.assertRaisesRegex(RuntimeError, "context:fact_check_cache"):
                    ai_review.run_ai_review(folder, persist=False)
            cache.assert_not_called()

    def test_status_binding_rejects_concurrent_review_input_edit(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            review = {
                "passed": True, "issues": [], "fact_checks": [],
                "reviewer": {},
                "reviewed_files": ai_review.reviewed_hashes(folder),
                "review_context": ai_review.review_context_hashes(folder),
            }
            real_transition = ai_review.review_status_transition

            def concurrent_transition(target, passed):
                transition = real_transition(target, passed)
                (folder / "讲书稿.md").write_text(
                    "concurrent semantic edit", encoding="utf-8")
                return transition

            with patch.object(ai_review, "run_ai_review", return_value=review), \
                    patch.object(
                        ai_review, "review_status_transition",
                        side_effect=concurrent_transition), \
                    patch.object(ai_review, "update_cache_from_review") as cache:
                with self.assertRaisesRegex(RuntimeError, "输入发生变化"):
                    ai_review.review_episode(folder)
            cache.assert_not_called()

    def test_status_binding_rejects_noncanonical_source_write(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            review = {
                "passed": True, "issues": [], "fact_checks": [],
                "reviewer": {},
                "reviewed_files": ai_review.reviewed_hashes(folder),
                "review_context": ai_review.review_context_hashes(folder),
            }
            real_transition = ai_review.review_status_transition

            def tampered_transition(target, passed):
                payload, source_text, apply_update = real_transition(
                    target, passed)

                def tampered_apply(folder_arg, payload_arg, source_arg):
                    result = apply_update(folder_arg, payload_arg, source_arg)
                    (Path(folder_arg) / "来源.md").write_text(
                        source_arg + "\n未审查改动", encoding="utf-8")
                    return result

                return payload, source_text, tampered_apply

            with patch.object(ai_review, "run_ai_review", return_value=review), \
                    patch.object(
                        ai_review, "review_status_transition",
                        side_effect=tampered_transition), \
                    patch.object(ai_review, "update_cache_from_review") as cache:
                with self.assertRaisesRegex(RuntimeError, "超出预期"):
                    ai_review.review_episode(folder)
            cache.assert_not_called()

    def test_cache_updates_only_after_authoritative_review_write(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            review = {
                "passed": True, "issues": [], "fact_checks": [],
                "reviewer": {},
                "reviewed_files": ai_review.reviewed_hashes(folder),
                "review_context": ai_review.review_context_hashes(folder),
            }

            def cache_after_commit(target, _review):
                self.assertTrue((Path(target) / "ai_review.json").exists())
                return 0

            with patch.object(ai_review, "run_ai_review", return_value=review), \
                    patch.object(
                        ai_review, "update_cache_from_review",
                        side_effect=cache_after_commit) as cache:
                result = ai_review.review_episode(folder)
            self.assertTrue(result["passed"])
            cache.assert_called_once()


if __name__ == "__main__":
    unittest.main()
