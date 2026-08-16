import inspect
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_review
import catalog
import catalog_core
import catalog_site
import catalog_publish
import fact_check_cache
import process as pipeline_process
import tts
from content_map import enrich_summary_map_evidence


class StableContractTests(unittest.TestCase):
    def test_source_identity_ignores_non_root_trailing_slash(self):
        self.assertEqual(
            pipeline_process._source_identity("http://EXAMPLE.com/a/"),
            pipeline_process._source_identity("https://example.com/a"),
        )
        self.assertEqual(
            pipeline_process._source_identity("https://example.com/"),
            "https://example.com/",
        )

    def test_catalog_facade_has_no_runtime_rebinding_layer(self):
        source = Path(catalog.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_sync_core", source)
        self.assertNotIn("_sync_site", source)
        self.assertNotIn("_sync_publish", source)
        self.assertNotIn("RLock", source)

    def test_site_uses_canonical_episode_directory_scanner(self):
        self.assertIs(catalog_site._episode_dirs, catalog_core._episode_dirs)

    def test_single_publish_reuses_r2_upload_primitive(self):
        source = inspect.getsource(catalog_publish._finish_impl)
        self.assertIn("_upload_r2_item", source)
        self.assertNotIn('"r2", "object", "put"', source)


class CatalogStatsCacheTests(unittest.TestCase):
    def test_ffprobe_is_cached_per_file_revision(self):
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "episode.mp3"
            mp3.write_bytes(b"x" * 4096)
            catalog_core._AUDIO_DURATION_CACHE.clear()
            result = SimpleNamespace(returncode=0, stdout="1200\n")
            with patch.object(
                    catalog_core.subprocess, "run", return_value=result) as run:
                self.assertEqual(catalog_core._audio_duration_minutes(mp3), 20)
                self.assertEqual(catalog_core._audio_duration_minutes(mp3), 20)
                self.assertEqual(run.call_count, 1)
                mp3.write_bytes(b"y" * 8192)
                self.assertEqual(catalog_core._audio_duration_minutes(mp3), 20)
                self.assertEqual(run.call_count, 2)


class EvidenceIntegrityTests(unittest.TestCase):
    def test_summary_enrichment_does_not_self_certify_notes_claims(self):
        content_map = {
            "units": [{"id": "U0001", "claims": ["claim"]}],
        }
        summary = enrich_summary_map_evidence(
            {"chapters": []}, "notes", content_map)
        self.assertEqual(summary["notes_claim_ids"], [])

    def test_fact_cache_context_reads_only_exact_fresh_claims(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            fact = {
                "claim": "Company A was founded in 2020",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "supported",
                "publication_status": "used_as_fact",
                "checked_at": "2026-08-15T00:00:00+00:00",
                "notes": "official registry",
            }
            fact_check_cache.store_fact_check(
                folder, fact, "https://example.com/company")
            context = fact_check_cache.build_cache_context(
                folder,
                ["Company A was founded in 2020", "different claim"],
                now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            )
            self.assertFalse(context["authoritative"])
            self.assertEqual(len(context["matched_entries"]), 1)
            self.assertEqual(
                context["matched_entries"][0]["source_url"],
                "https://example.com/company",
            )

    def test_ai_review_materializes_structured_cache_context(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "content_map.json").write_text(json.dumps({
                "units": [{"claims": ["A stable public fact"]}],
            }), encoding="utf-8")
            fact_check_cache.store_fact_check(folder, {
                "claim": "A stable public fact",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "supported",
                "publication_status": "used_as_fact",
                "checked_at": "2026-08-15T00:00:00+00:00",
                "notes": "source",
            }, "https://example.com/fact")
            context_path = ai_review._write_cache_review_context(folder)
            payload = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["matched_entries"]), 1)
            self.assertFalse(payload["authoritative"])


class LegacyTtsTests(unittest.TestCase):
    def test_backfill_inventories_audio_but_does_not_certify_it(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = folder / "讲书稿.md"
            briefing.write_text(
                "这是足够长的开场内容。\n\n## 第一章\n这是足够长的章节正文。",
                encoding="utf-8",
            )
            plan = tts.build_tts_plan(folder, briefing.name)
            audio = folder / "audio"
            audio.mkdir()
            for item in plan:
                (audio / item["filename"]).write_bytes(b"a" * 2048)
            (folder / "Episode.mp3").write_bytes(b"b" * 4096)

            result = tts.backfill_tts_manifest(
                folder, briefing.name, "Episode")
            manifest = json.loads(
                (folder / "tts_manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result.ok)
            self.assertFalse(manifest["completed"])
            self.assertEqual(
                manifest["verification_status"], "legacy_unverified")
            self.assertTrue(any(
                "未证明" in error
                for error in tts.validate_tts_manifest(
                    folder, briefing.name, "Episode")))


if __name__ == "__main__":
    unittest.main()
