import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fact_check_cache
import ai_review
import review_repair


class ReviewRepairTests(unittest.TestCase):
    def test_safe_summary_issue_is_repaired_then_independently_rereviewed(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            failed = {
                "passed": False,
                "issues": [{
                    "severity": "high",
                    "category": "summary_map",
                    "file": "summary_map.json",
                    "statement": "hash stale",
                }],
                "reviewed_files": {"讲书稿.md": "old"},
            }
            passed = {
                "passed": True,
                "issues": [],
                "reviewed_files": {"讲书稿.md": "new"},
            }
            reviewer = unittest.mock.Mock(side_effect=[failed, passed])
            with patch(
                    "review_repair._repair_summary",
                    return_value={"action": "finalize_content_package"}) as repair:
                result = review_repair.review_and_repair(
                    folder, reviewer=reviewer, max_rounds=2)

            self.assertTrue(result["passed"])
            self.assertEqual(reviewer.call_count, 2)
            repair.assert_called_once_with(folder)
            log = json.loads(
                (folder / "review_repair.json").read_text(encoding="utf-8"))
            self.assertTrue(log["passed"])
            self.assertEqual(len(log["history"]), 2)

    def test_factuality_issue_is_never_auto_marked_passed(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            failed = {
                "passed": False,
                "issues": [{
                    "severity": "high",
                    "category": "factuality",
                    "file": "讲书稿.md",
                    "statement": "unsupported medical claim",
                }],
                "reviewed_files": {},
            }
            reviewer = unittest.mock.Mock(return_value=failed)
            result = review_repair.review_and_repair(
                folder, reviewer=reviewer, max_rounds=2)

            self.assertFalse(result["passed"])
            self.assertEqual(reviewer.call_count, 1)
            log = json.loads(
                (folder / "review_repair.json").read_text(encoding="utf-8"))
            self.assertIn(
                "禁止无证据自动修复",
                log["history"][0]["repair_blockers"][0],
            )

    def test_evidence_repair_requires_explicit_unit_ids(self):
        review = {
            "passed": False,
            "issues": [{
                "severity": "high",
                "category": "evidence_integrity",
                "statement": "claim mapping is vague",
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "禁止猜测"):
                review_repair._repair_evidence(Path(td), review["issues"])


class FactCheckCacheTests(unittest.TestCase):
    def test_dynamic_cache_entry_expires_but_historical_entry_persists(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            checked_at = "2026-01-01T00:00:00+00:00"
            fact = {
                "claim": "当前估值为一百亿美元",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "supported",
                "publication_status": "used_as_fact",
                "checked_at": checked_at,
                "notes": "source",
            }
            fact_check_cache.store_fact_check(
                folder, fact, "https://example.com", dynamic=True)
            now = datetime(2026, 1, 20, tzinfo=timezone.utc)
            self.assertIsNone(fact_check_cache.get_cached_fact_check(
                folder, fact["claim"], "https://example.com",
                dynamic=True, ttl_days=7, now=now,
            ))
            self.assertIsNotNone(fact_check_cache.get_cached_fact_check(
                folder, fact["claim"], "https://example.com",
                dynamic=False, ttl_days=7,
                now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ))

    def test_cache_key_binds_claim_source_and_source_date(self):
        first = fact_check_cache.cache_key(
            "A claim", "https://example.com", "2026-01-01")
        second = fact_check_cache.cache_key(
            "A claim", "https://example.com", "2026-01-02")
        third = fact_check_cache.cache_key(
            "Another claim", "https://example.com", "2026-01-01")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_guest_firsthand_claim_is_not_written_to_external_fact_cache(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            review = {
                "fact_checks": [{
                    "claim": "嘉宾称内部指标提高百分之二十",
                    "claim_type": "guest_firsthand",
                    "verification_mode": "transcript_attribution",
                    "verdict": "faithfully_attributed",
                    "publication_status": "attributed_or_qualified",
                    "checked_at": "2026-08-15T00:00:00+00:00",
                    "source_urls": ["https://example.com/irrelevant"],
                    "notes": "嘉宾一手信息",
                }],
            }
            stored = fact_check_cache.update_cache_from_review(folder, review)
            cache = fact_check_cache.load_cache(folder)
            self.assertEqual(stored, 0)
            self.assertEqual(cache["entries"], {})


class PartialReviewTests(unittest.TestCase):
    def test_review_scope_identifies_only_changed_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for name in ai_review.REVIEW_FILES:
                (folder / name).write_text(name, encoding="utf-8")
            hashes = ai_review.reviewed_hashes(folder)
            (folder / "ai_review.json").write_text(json.dumps({
                "passed": True,
                "reviewed_files": hashes,
            }), encoding="utf-8")
            (folder / "讲书稿.md").write_text("changed", encoding="utf-8")

            scope = ai_review.review_scope(folder)
            prompt = ai_review._prompt(folder, scope)

            self.assertEqual(scope["mode"], "partial_then_full")
            self.assertEqual(scope["changed_files"], ["讲书稿.md"])
            self.assertIn("不得直接继承 passed", prompt)
            self.assertIn("完整发布判定", prompt)

    def test_review_prompt_does_not_require_web_for_guest_firsthand_claims(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = ai_review._prompt(Path(td), {
                "mode": "full",
                "changed_files": [],
                "unchanged_files": [],
                "previous_passed": False,
            })
        self.assertIn("不强制联网", prompt)
        self.assertIn("不能因无公开网页而扣 factuality 分", prompt)
        self.assertIn("实体准确性作为独立硬门", prompt)


if __name__ == "__main__":
    unittest.main()
