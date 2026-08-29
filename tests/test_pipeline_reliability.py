import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ai_review
import agent_pipeline
import catalog as catalog_cli
import catalog_core
import catalog_publish
import catalog_site
from canonical_entities import (
    public_entity_alias_errors,
    validate_canonical_entities,
)
from editorial_corrections import validate_editorial_corrections
from content_map import (
    apply_claim_evidence_mapping,
    coverage_report,
    enrich_content_map_evidence,
    normalize_detail_items,
    unit_detail_ids,
    validate_content_map,
)
from episode import _source_heading
import claim_evidence
from rebuild_plan import build_rebuild_plan
from source_relevance import (
    expected_source_references,
    normalize_source_url,
    refresh_source_relevance_cache,
    validate_source_relevance_cache,
)
from tts import build_tts_plan


def review_payload():
    return {
        "schema_version": 3,
        "passed": True,
        "summary": "review summary",
        "transcript_quality": {"passed": True, "score": 95},
        "coverage": {"passed": True, "score": 95},
        "factuality": {"passed": True, "score": 95},
        "numbers": {"passed": True},
        "attribution": {"passed": True},
        "entity_accuracy": {"passed": True, "checked_entities": [], "issues": []},
        "tts": {"passed": True},
        "publish": {"passed": True},
        "issues": [],
        "fact_checks": [{
            "claim": "嘉宾提出用产品责任促进 AI 安全",
            "parent_claim_id": "U0001-C01",
            "subclaim_id": "U0001-C01-F01",
            "claim_type": "guest_opinion",
            "claim_origin": "speaker_firsthand",
            "speaker_role": "guest",
            "assertion_type": "recommendation",
            "verification_mode": "transcript_attribution",
            "risk_domain": "legal",
            "verdict": "qualified",
            "publication_status": "attributed_or_qualified",
            "evidence_segment_ids": ["S0001"],
            "source_urls": [],
            "checked_at": "2026-08-23T00:00:00Z",
            "notes": "节目中的治理建议。",
        }],
    }


class CondensedCoverageTests(unittest.TestCase):
    def test_condensed_claim_is_required_in_notes_but_optional_in_briefing(self):
        content_map = {
            "units": [
                {
                    "id": "U0001", "status": "included",
                    "importance": "high", "claims": ["required"],
                },
                {
                    "id": "U0002", "status": "condensed",
                    "importance": "medium", "claims": ["notes only"],
                },
            ],
        }
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "chapter",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
            "notes_claim_ids": ["U0001-C01", "U0002-C01"],
        }
        report = coverage_report(content_map, summary)
        self.assertTrue(report["passed"])
        self.assertEqual(report["medium_total"], 0)
        self.assertEqual(report["claim_total"], 1)
        self.assertEqual(report["notes_claim_total"], 2)


class CorrectedClaimEvidenceTests(unittest.TestCase):
    def test_corrected_paragraphs_align_to_segment_ids(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "转录_纠错.txt").write_text(
                "corrected one\n\ncorrected two", encoding="utf-8")
            transcript = {
                "segments": [
                    {"id": "S0001", "text": "raw one"},
                    {"id": "S0002", "text": "raw two"},
                ],
            }
            aligned = claim_evidence._corrected_segment_texts(
                folder, transcript)
            payloads = claim_evidence._unit_payloads({
                "units": [{
                    "id": "U0001", "status": "included",
                    "topic": "topic", "claims": ["claim"],
                    "evidence": {"segment_ids": ["S0002"]},
                }],
            }, transcript, folder=folder)
        self.assertEqual(aligned["S0002"], "corrected two")
        self.assertEqual(
            payloads[0]["segments"][0]["corrected_text"],
            "corrected two")

    def test_low_confidence_unit_is_retried_at_max_effort(self):
        batch = [{
            "unit_id": "U0001",
            "claims": [{"claim_id": "U0001-C01", "text": "claim"}],
            "segments": [{"id": "S0001", "text": "source"}],
        }]
        low = [{
            "claim_id": "U0001-C01", "segment_ids": ["S0001"],
            "confidence": "low", "rationale": "insufficient initial support",
        }]
        high = [{
            "claim_id": "U0001-C01", "segment_ids": ["S0001"],
            "confidence": "high", "rationale": "source directly supports claim",
        }]
        with mock.patch.object(
                claim_evidence, "_run_batch",
                return_value=({"claims": high}, {"duration_ms": 1})) as runner:
            mappings, wrappers = claim_evidence._retry_low_confidence_units(
                Path("/tmp"), batch, low, "model", 1)
        self.assertEqual(mappings, high)
        self.assertEqual(len(wrappers), 1)
        self.assertEqual(runner.call_args.args[3], "max")

    def test_final_low_confidence_marks_invalid_result(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            transcript = {
                "evidence": {"revision_sha256": "revision"},
                "meta": {"timestamped": True, "evidence_mode": "timestamp"},
                "segments": [
                    {"id": "S0001", "start": 0, "end": 5, "text": "one"},
                    {"id": "S0002", "start": 5, "end": 10, "text": "two"},
                ],
            }
            content_map = {
                "schema_version": 3,
                "evidence_mode": "timestamp",
                "units": [{
                    "id": "U0001", "topic": "topic",
                    "claims": ["one", "two"],
                    "importance": "high", "status": "included",
                    "timestamps": [[0, 10]],
                }],
            }
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")

            def low_result(_folder, batch, _model, _effort, _index):
                claims = [{
                    "claim_id": claim["claim_id"],
                    "segment_ids": [batch[0]["segments"][0]["id"]],
                    "confidence": "low",
                    "rationale": "source remains insufficient after review",
                } for claim in batch[0]["claims"]]
                return {"claims": claims}, {"duration_ms": 1}

            with mock.patch.object(
                    claim_evidence, "_run_batch", side_effect=low_result):
                with self.assertRaisesRegex(RuntimeError, "confidence=low"):
                    claim_evidence.refine_claim_evidence(
                        folder, concurrency=1, max_batch_chars=100000)
            progress = json.loads(
                (folder / claim_evidence.PROGRESS_FILENAME).read_text(
                    encoding="utf-8"))
        self.assertEqual(progress["status"], "invalid_result")
        self.assertEqual(progress["failed_unit_ids"], ["U0001"])
        self.assertEqual(progress["completed_unit_ids"], [])


class LowConfidenceClaimRepairTests(unittest.TestCase):
    def test_repair_updates_only_named_unit_and_modalities(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = {
                "segments": [{"id": "S0001", "text": "direct support"}],
            }
            content_map = {
                "units": [
                    {
                        "id": "U0001", "topic": "topic",
                        "claims": ["overbroad claim"],
                        "claim_modalities": ["general_claim"],
                        "evidence": {"segment_ids": ["S0001"]},
                    },
                    {
                        "id": "U0002", "topic": "untouched",
                        "claims": ["keep"],
                        "claim_modalities": ["actual_event"],
                        "evidence": {"segment_ids": ["S0001"]},
                    },
                ],
            }
            (folder / "transcript.raw.json").write_text(
                json.dumps(raw), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with mock.patch.object(agent_pipeline, "run_json_task", return_value={
                "payload": {"units": [{
                    "unit_id": "U0001",
                    "claims": ["direct support"],
                    "claim_modalities": ["actual_event"],
                }]},
            }):
                repaired = agent_pipeline._repair_low_confidence_claims(
                    folder,
                    folder / "transcript.raw.json",
                    RuntimeError("U0001-C01: confidence=low"),
                )
            result = json.loads(
                (folder / "content_map.json").read_text(encoding="utf-8"))
        self.assertEqual(repaired, ["U0001"])
        self.assertEqual(result["units"][0]["claims"], ["direct support"])
        self.assertEqual(result["units"][1]["claims"], ["keep"])


class EvidenceEnrichmentSafetyTests(unittest.TestCase):
    def test_open_ended_last_unit_window_is_normalized_without_erasing_evidence(self):
        transcript = {
            "meta": {"timestamped": True, "evidence_mode": "timestamp"},
            "segments": [{
                "id": "S0001", "start": 12, "end": None,
                "text": "final segment",
            }],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "timestamp",
            "units": [{
                "id": "U0001",
                "timestamps": [[12, None]],
                "evidence": {
                    "mode": "timestamp",
                    "segment_ids": ["S0001"],
                    "source_sha256": "existing",
                },
            }],
        }
        enriched, _transcript = enrich_content_map_evidence(
            content_map, transcript)
        self.assertEqual(enriched["units"][0]["timestamps"], [[12, 12]])
        self.assertEqual(
            enriched["units"][0]["evidence"]["segment_ids"],
            ["S0001"],
        )

    def test_unknown_text_anchor_cannot_enrich_to_empty_evidence(self):
        transcript = {
            "meta": {"timestamped": False, "evidence_mode": "text_anchor"},
            "segments": [{"id": "S0001", "text": "source"}],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "text_anchor",
            "units": [{
                "id": "U0001",
                "evidence": {"segment_ids": ["S9999"]},
            }],
        }
        with self.assertRaisesRegex(ValueError, "enrichment 结果为空"):
            enrich_content_map_evidence(content_map, transcript)

    def test_claim_payload_without_segments_fails_before_runner(self):
        with self.assertRaisesRegex(RuntimeError, "不重试"):
            claim_evidence._validate_payload_sources([{
                "unit_id": "U0001",
                "claims": [{"claim_id": "U0001-C01", "text": "claim"}],
                "segments": [],
            }])


class ClaimEvidenceProgressTests(unittest.TestCase):
    def test_failed_unit_writes_partial_progress_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            transcript = {
                "evidence": {"revision_sha256": "revision"},
                "meta": {"timestamped": True, "evidence_mode": "timestamp"},
                "segments": [{
                    "id": "S0001", "start": 0, "end": 5, "text": "source",
                }],
            }
            content_map = {
                "schema_version": 3,
                "evidence_mode": "timestamp",
                "units": [{
                    "id": "U0001", "topic": "topic",
                    "claims": ["claim one", "claim two"],
                    "importance": "high", "status": "included",
                    "timestamps": [[0, 5]],
                }],
            }
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with mock.patch.object(
                    claim_evidence, "_run_batch",
                    side_effect=RuntimeError("runner down")):
                with self.assertRaises(RuntimeError):
                    claim_evidence.refine_claim_evidence(
                        folder, concurrency=1, max_batch_chars=100000)
            progress = json.loads(
                (folder / claim_evidence.PROGRESS_FILENAME).read_text(
                    encoding="utf-8"))
        self.assertEqual(progress["status"], "partial")
        self.assertEqual(progress["evidence_revision"], "revision")
        self.assertEqual(progress["failed_unit_ids"], ["U0001"])
        self.assertEqual(progress["pending_unit_ids"], ["U0001"])
        self.assertTrue(claim_evidence.validate_progress(progress, transcript))

    def test_completed_progress_matches_revision(self):
        transcript = {"evidence": {"revision_sha256": "revision"}}
        payload = {
            "schema_version": 1,
            "status": "completed",
            "evidence_revision": "revision",
            "target_unit_ids": ["U0001"],
            "completed_unit_ids": ["U0001"],
            "pending_unit_ids": [],
            "failed_unit_ids": [],
        }
        self.assertEqual(
            claim_evidence.validate_progress(payload, transcript), [])
        payload["evidence_revision"] = "stale"
        self.assertTrue(any(
            "revision" in error
            for error in claim_evidence.validate_progress(payload, transcript)
        ))


class PodcastRootTests(unittest.TestCase):
    def test_catalog_cli_binds_all_modules_to_configured_root(self):
        old = catalog_core.CatalogPaths(
            base_dir=catalog_publish.BASE_DIR,
            content_dir=catalog_publish.CONTENT_DIR,
            site_dir=catalog_publish.SITE_DIR,
            catalog=catalog_publish.CATALOG,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content = root / "private-content"
            site = root / "private-site"
            catalog_path = content / "catalog.md"
            try:
                with mock.patch.object(catalog_cli, "CONFIG_ROOT", root), \
                        mock.patch.object(
                            catalog_cli, "CONFIG_CONTENT_DIR", content), \
                        mock.patch.object(
                            catalog_cli, "CONFIG_SITE_DIR", site), \
                        mock.patch.object(
                            catalog_cli, "CONFIG_CATALOG", catalog_path):
                    paths = catalog_cli._configure_cli_paths()
                self.assertEqual(paths.base_dir, root)
                self.assertEqual(catalog_core.CONTENT_DIR, content)
                self.assertEqual(catalog_site.SITE_DIR, site)
                self.assertEqual(catalog_publish.CATALOG, catalog_path)
            finally:
                catalog_publish.configure_paths(old)
                catalog_cli.BASE_DIR = old.base_dir
                catalog_cli.CONTENT_DIR = old.content_dir
                catalog_cli.SITE_DIR = old.site_dir
                catalog_cli.CATALOG = old.catalog


class SourceLabelTests(unittest.TestCase):
    def test_source_heading_matches_source_kind(self):
        self.assertEqual(_source_heading("web_transcript"), "转录页面")
        self.assertEqual(_source_heading("third_party_transcript"), "转录页面")
        self.assertEqual(_source_heading("local_asr"), "原始音频")
        self.assertEqual(_source_heading("local_transcript"), "本地转录来源")


class ClaimEvidenceRoleTests(unittest.TestCase):
    def test_primary_and_context_evidence_are_bound_separately(self):
        transcript = {
            "meta": {"timestamped": True, "evidence_mode": "timestamp"},
            "segments": [
                {"id": "S0001", "start": 0, "end": 5, "text": "claim"},
                {"id": "S0002", "start": 5, "end": 10, "text": "context"},
            ],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "timestamp",
            "units": [{
                "id": "U0001",
                "topic": "topic",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 10]],
            }],
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        content_map, transcript = apply_claim_evidence_mapping(
            content_map,
            transcript,
            [{
                "claim_id": "U0001-C01",
                "segment_ids": ["S0001", "S0002"],
                "primary_segment_ids": ["S0001"],
                "context_segment_ids": ["S0002"],
                "confidence": "high",
                "rationale": "S0001 directly supports the claim; S0002 adds context.",
            }],
        )
        roles = content_map["units"][0]["claim_evidence_roles"]["C01"]
        self.assertEqual(roles["primary_segment_ids"], ["S0001"])
        self.assertEqual(roles["context_segment_ids"], ["S0002"])
        self.assertEqual(validate_content_map(content_map, transcript)[0], [])


class RebuildPlanTests(unittest.TestCase):
    def test_transcript_basis_change_is_planned_in_active_mode(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for name in (
                    "transcript.raw.json", "content_map.json",
                    "中文完整笔记.md", "讲书稿.md"):
                (folder / name).write_text("{}", encoding="utf-8")
            (folder / "原始转录.txt").write_text("source", encoding="utf-8")
            (folder / "转录_纠错.txt").write_text("corrected", encoding="utf-8")
            (folder / "summary_map.json").write_text(json.dumps({
                "transcript_basis": {
                    "file": "转录_纠错.txt",
                    "sha256": "stale",
                },
                "chapters": [],
            }), encoding="utf-8")
            plan = build_rebuild_plan(folder)
        self.assertTrue(plan["needs_content"])
        self.assertIn("stale:transcript_basis", plan["reasons"])
        self.assertEqual(plan["mode"], "active")
        self.assertEqual(plan["schema_version"], 2)
        self.assertIn("ai_review", plan["stages"])


class CanonicalEntityTests(unittest.TestCase):
    def test_entity_contract_rejects_alias_leak_in_public_text(self):
        transcript = {"segments": [{"id": "S0001", "text": "Edward Lameh"}]}
        payload = {
            "schema_version": 1,
            "entities": [{
                "entity_id": "EN0001",
                "canonical_name": "Edward Lemay",
                "observed_names": ["Edward Lameh", "Edward LeMay"],
                "public_aliases": [],
                "entity_type": "person",
                "source_urls": ["https://example.com/edward-lemay"],
                "segment_ids": ["S0001"],
                "confidence": "high",
                "rationale": "Official faculty profile confirms the canonical spelling.",
            }],
        }
        self.assertEqual(validate_canonical_entities(payload, transcript), [])
        errors = public_entity_alias_errors(
            payload, "Interview with Edward LeMay")
        self.assertTrue(any("Edward LeMay" in error for error in errors))
        self.assertEqual(
            public_entity_alias_errors(payload, "Interview with Edward Lemay"), [])
    def test_short_observed_name_inside_canonical_name_is_not_a_leak(self):
        transcript = {"segments": [{"id": "S0001", "text": "Neal Gabler"}]}
        payload = {
            "schema_version": 1,
            "entities": [{
                "entity_id": "EN0001",
                "canonical_name": "Neal Gabler",
                "observed_names": ["Neal", "Neal Gabler"],
                "public_aliases": [],
                "entity_type": "person",
                "source_urls": ["https://example.com/neal-gabler"],
                "segment_ids": ["S0001"],
                "confidence": "high",
                "rationale": "Official profile confirms the complete canonical name.",
            }],
        }
        self.assertEqual(validate_canonical_entities(payload, transcript), [])
        self.assertEqual(public_entity_alias_errors(payload, "Neal Gabler wrote it."), [])
        self.assertTrue(public_entity_alias_errors(payload, "Neal wrote it."))

    def test_sourced_localized_public_alias_is_allowed(self):
        transcript = {"segments": [{"id": "S0001", "text": "Walt Disney"}]}
        payload = {
            "schema_version": 1,
            "entities": [{
                "entity_id": "EN0001",
                "canonical_name": "Walt Disney",
                "observed_names": ["Walt Disney"],
                "public_aliases": ["华特·迪士尼"],
                "entity_type": "person",
                "source_urls": ["https://example.com/walt-disney"],
                "segment_ids": ["S0001"],
                "confidence": "high",
                "rationale": "Official profile supports the localized public name.",
            }],
        }
        self.assertEqual(validate_canonical_entities(payload, transcript), [])
        self.assertEqual(
            public_entity_alias_errors(payload, "华特·迪士尼创办了公司。"), [])


class DetailItemCoverageTests(unittest.TestCase):
    def test_low_priority_detail_may_be_mapped_when_notes_use_it(self):
        content_map = normalize_detail_items({
            "units": [
                {
                    "id": "U0001", "status": "included", "importance": "high",
                    "claims": ["required"], "numbers": ["required number"],
                    "examples": [],
                },
                {
                    "id": "U0002", "status": "condensed", "importance": "low",
                    "claims": ["optional"], "numbers": ["optional number"],
                    "examples": [],
                },
            ],
        })
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "chapter", "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
            "notes_claim_ids": ["U0001-C01", "U0002-C01"],
            "notes_number_ids": ["U0001-N01", "U0002-N01"],
            "notes_example_ids": [],
        }
        self.assertTrue(coverage_report(content_map, summary)["passed"])

    def test_number_and_example_ids_are_stable_and_required_in_notes(self):
        content_map = normalize_detail_items({
            "units": [{
                "id": "U0001", "status": "included", "importance": "high",
                "claims": ["claim"],
                "numbers": ["2009", "70%"],
                "examples": ["example"],
            }],
        })
        self.assertEqual(
            unit_detail_ids(content_map, "number"),
            ["U0001-N01", "U0001-N02"],
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "chapter", "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
            "notes_claim_ids": ["U0001-C01"],
            "notes_number_ids": ["U0001-N01", "U0001-N02"],
            "notes_example_ids": ["U0001-E01"],
        }
        report = coverage_report(content_map, summary)
        self.assertTrue(report["passed"])
        self.assertEqual(report["notes_number_coverage"], 1.0)
        summary["notes_example_ids"] = []
        report = coverage_report(content_map, summary)
        self.assertFalse(report["passed"])
        self.assertEqual(report["notes_missing_example_ids"], ["U0001-E01"])


class SourceRelevanceTests(unittest.TestCase):
    def test_cache_materializes_title_excerpt_hash_and_references(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "editorial_corrections.json").write_text(json.dumps({
                "schema_version": 1,
                "corrections": [{
                    "correction_id": "EC0001",
                    "source_urls": ["https://example.com/study"],
                }],
            }), encoding="utf-8")

            def fetcher(url):
                return {
                    "status": "fetched", "http_status": 200,
                    "final_url": url, "content_type": "text/html",
                    "title": "Relationship study",
                    "excerpt": "This study examines relationship satisfaction.",
                    "content_sha256": "a" * 64,
                }

            payload = refresh_source_relevance_cache(
                folder, fetcher=fetcher)
            refs = expected_source_references(folder)
        self.assertEqual(validate_source_relevance_cache(payload, refs), [])
        entry = payload["entries"]["https://example.com/study"]
        self.assertEqual(entry["source_ids"], ["EC0001"])
        self.assertEqual(entry["title"], "Relationship study")

    def test_tracking_parameters_are_removed_from_cache_identity(self):
        self.assertEqual(
            normalize_source_url(
                "HTTPS://Example.COM/report.pdf?utm_source=x&cid=abc#page=2"),
            "https://example.com/report.pdf",
        )

        self.assertEqual(
            normalize_source_url(
                "https://example.com/data?source=primary&utm_medium=email"),
            "https://example.com/data?source=primary",
        )

    def test_recent_fetch_error_is_reused_until_retry_ttl(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "editorial_corrections.json").write_text(json.dumps({
                "schema_version": 1,
                "corrections": [{
                    "correction_id": "EC0001",
                    "source_urls": ["https://example.com/unavailable"],
                }],
            }), encoding="utf-8")
            attempts = []

            def fetcher(url):
                attempts.append(url)
                raise TimeoutError("temporary timeout")

            first = refresh_source_relevance_cache(folder, fetcher=fetcher)
            first_text = (folder / "source_relevance_cache.json").read_text(
                encoding="utf-8")
            second = refresh_source_relevance_cache(folder, fetcher=fetcher)
            second_text = (folder / "source_relevance_cache.json").read_text(
                encoding="utf-8")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(first_text, second_text)
        self.assertEqual(first["entries"], second["entries"])
        entry = second["entries"]["https://example.com/unavailable"]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_kind"], "TimeoutError")

    def test_cache_rejects_failed_or_unreferenced_entries(self):
        refs = {"https://example.com/study": ["EC0001"]}
        payload = {
            "schema_version": 1,
            "entries": {
                "https://example.com/study": {
                    "status": "error", "error": "timeout",
                    "source_ids": ["EC0001"], "content_sha256": "",
                    "title": "", "excerpt": "",
                },
                "https://example.com/extra": {
                    "status": "fetched", "source_ids": [],
                    "content_sha256": "b" * 64, "title": "extra",
                    "excerpt": "extra",
                },
            },
        }
        errors = validate_source_relevance_cache(payload, refs)
        self.assertTrue(any("抓取失败" in error for error in errors))
        self.assertTrue(any("未引用 URL" in error for error in errors))


class EditorialCorrectionTests(unittest.TestCase):
    def test_correction_requires_real_claim_and_source(self):
        payload = {
            "schema_version": 1,
            "corrections": [{
                "correction_id": "EC0001",
                "claim_id": "U0001-C01",
                "episode_statement": "节目称股份进入信托。",
                "public_treatment": "公开资料只支持治理股票。",
                "verdict": "corrected",
                "source_urls": ["https://example.com/official"],
                "checked_at": "2026-08-23T00:00:00Z",
                "risk_domain": "financial",
            }],
        }
        self.assertEqual(
            validate_editorial_corrections(
                payload, valid_claim_ids={"U0001-C01"}), [])
        payload["corrections"][0]["source_urls"] = []
        errors = validate_editorial_corrections(
            payload, valid_claim_ids={"U0001-C01"})
        self.assertTrue(any("公开来源" in error for error in errors))


class ReviewMechanicalRetryTests(unittest.TestCase):
    def _workspace(self, folder):
        (folder / "content_map.json").write_text(json.dumps({
            "units": [{"id": "U0001", "claims": ["claim"]}],
        }), encoding="utf-8")

    def test_mechanical_retry_fixes_contract_without_changing_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._workspace(folder)
            first = review_payload()
            corrected = copy.deepcopy(first)
            check = corrected["fact_checks"][0]
            check["verification_mode"] = "safety_cross_check"
            check["source_urls"] = ["https://example.com/legal-safety"]
            with mock.patch.object(ai_review, "run_json_task", return_value={
                "payload": corrected,
                "duration_ms": 1,
                "retry_count": 0,
            }) as runner:
                result, audit, wrapper = ai_review._validate_or_retry_review(
                    folder, first, model="", effort="max")
        self.assertEqual(audit["retry_count"], 1)
        self.assertEqual(audit["final_errors"], [])
        self.assertEqual(
            result["fact_checks"][0]["verification_mode"],
            "safety_cross_check",
        )
        self.assertIsNotNone(wrapper)
        runner.assert_called_once()

    def test_mechanical_retry_cannot_flip_review_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._workspace(folder)
            first = review_payload()
            corrected = copy.deepcopy(first)
            corrected["passed"] = False
            corrected["fact_checks"][0]["verification_mode"] = "safety_cross_check"
            corrected["fact_checks"][0]["source_urls"] = [
                "https://example.com/legal-safety"]
            with mock.patch.object(ai_review, "run_json_task", return_value={
                "payload": corrected,
            }):
                with self.assertRaisesRegex(RuntimeError, "语义结论"):
                    ai_review._validate_or_retry_review(
                        folder, first, model="", effort="max")


class TtsDisplaySpokenTests(unittest.TestCase):
    def test_plan_binds_display_and_spoken_text_separately(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "这是足够长的导览文本。\n\n## a16z\n\na16z 投资了 AI。",
                encoding="utf-8",
            )
            (folder / "tts_lexicon.json").write_text(json.dumps({
                "a16z": "A 十六 Z",
                "AI": "A I",
            }), encoding="utf-8")
            plan = build_tts_plan(folder, "讲书稿.md")
        self.assertTrue(plan)
        self.assertTrue(all(item.get("display_sha256") for item in plan))
        self.assertTrue(all(item.get("spoken_sha256") for item in plan))
        self.assertTrue(any(
            item["display_sha256"] != item["spoken_sha256"]
            for item in plan
        ))


if __name__ == "__main__":
    unittest.main()
