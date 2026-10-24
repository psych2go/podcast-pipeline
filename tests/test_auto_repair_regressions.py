import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import agent_pipeline, preflight, source_relevance
from scripts.canonical_entities import public_entity_alias_errors
from scripts.content_map import enrich_content_map_evidence, enrich_summary_map_evidence


def test_missing_claim_mapping_is_attached_to_matching_unit_chapter():
    summary = {"chapters": [{"title": "Unit", "unit_ids": ["U0008"]}]}
    result = enrich_summary_map_evidence(
        summary,
        "notes",
        {"units": [{"id": "U0008", "claims": ["claim"],
                    "importance": "high", "status": "included"}]},
    )
    assert "U0008-C01" in result["notes_claim_ids"]
    assert "U0008-C01" in result["chapters"][0]["claim_ids"]


def test_person_short_name_is_allowed_after_canonical_name():
    entities = {"entities": [{
        "entity_type": "person", "canonical_name": "Arvind Krishna",
        "observed_names": ["Arvind"], "public_aliases": [],
    }]}
    assert public_entity_alias_errors(
        entities, "Arvind Krishna later said Arvind agreed."
    ) == []


def test_previous_segment_ids_recover_empty_timestamps():
    transcript = {"segments": [
        {"id": "S0001", "start": 0, "end": 1, "text": "evidence"},
    ]}
    content = {"units": [{"id": "U0001", "claims": ["claim"],
                           "timestamps": [],
                           "evidence": {"segment_ids": ["S0001"]}}]}
    enriched, _ = enrich_content_map_evidence(content, transcript)
    assert enriched["units"][0]["evidence"]["segment_ids"] == ["S0001"]


BAD_URL = "https://example.invalid/unconfirmed"
def _write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_resume_repairs_sources_then_rechecks_ai_review_before_gate():
    with TemporaryDirectory() as td:
        folder = Path(td)
        (folder / "content_map.json").write_text("{}", encoding="utf-8")
        reports = [
            {
                "passed": False,
                "errors": ["内容审查状态未通过: pending", "缺少 ai_review.json"],
                "error_details": [
                    {"code": "content_review_status", "message": "内容审查状态未通过: pending"},
                    {"code": "ai_review_missing", "message": "缺少 ai_review.json"},
                ],
            },
            {"passed": True, "errors": [], "error_details": []},
        ]
        repaired = []

        def repair(_folder, payload):
            repaired.append(payload)
            return {"removed_urls": [BAD_URL]}

        with patch.object(
            preflight, "refresh_source_relevance_cache", return_value={}
        ), patch.object(
            preflight, "repair_unconfirmed_source_citations", side_effect=repair
        ), patch.object(
            preflight, "build_quality_report", side_effect=reports
        ), patch("scripts.review_repair.review_and_repair") as review:
            assert preflight.quality_gate(folder) is True

        assert repaired
        review.assert_called_once()


def test_unconfirmed_source_is_removed_from_citations_and_restamped():
    with TemporaryDirectory() as td:
        folder = Path(td)
        _write_json(folder / "canonical_entities.json", {
            "schema_version": 1,
            "entities": [{
                "entity_id": "EN0001", "canonical_name": "Example Person",
                "observed_names": ["Example Person"], "public_aliases": [],
                "entity_type": "person", "source_urls": [BAD_URL],
                "segment_ids": ["S0001"], "confidence": "high", "rationale": "节目明确提到该人物。",
            }],
        })
        _write_json(folder / "editorial_fact_checks.json", {
            "schema_version": 1,
            "claims": [{"parent_claim_id": "U0001-C01", "requires_web": True,
                         "checks": [{"source_urls": [BAD_URL],
                                     "editorial_correction": "外部纠正"}]}],
            "issue_inventory": [{"source_urls": [BAD_URL]}],
        })
        (folder / "editorial_fact_check_batches").mkdir()
        _write_json(folder / "editorial_fact_check_batches" / "001.json", {
            "result": {"payload": {"claims": [{"checks": [{"source_urls": [BAD_URL]}]}]}},
        })
        (folder / "讲书稿.md").write_text(f"[来源]({BAD_URL})\n", encoding="utf-8")
        cache = {
            "schema_version": 1,
            "entries": {
                BAD_URL: {
                    "status": "fetched", "title": "Unrelated", "excerpt": "",
                    "content_sha256": "x", "relevance_status": "unconfirmed",
                    "source_ids": ["EN0001"],
                },
            },
        }

        result = source_relevance.repair_unconfirmed_source_citations(folder, cache)

        assert result["removed_urls"] == [BAD_URL]
        assert BAD_URL not in (folder / "canonical_entities.json").read_text(
            encoding="utf-8"
        )
        assert BAD_URL not in (folder / "editorial_fact_checks.json").read_text(
            encoding="utf-8"
        )
        assert BAD_URL not in (
            folder / "editorial_fact_check_batches" / "001.json"
        ).read_text(encoding="utf-8")
        assert BAD_URL not in (folder / "讲书稿.md").read_text(encoding="utf-8")
        assert source_relevance.expected_source_references(folder) == {}
        assert result["restamped"]
        assert not (folder / "quality_report.json").exists()


def test_depth_repair_request_does_not_feed_writable_briefing_as_input():
    with TemporaryDirectory() as td:
        folder = Path(td)
        for name in ("讲书稿.md", "summary_map.json", "content_map.json", "中文完整笔记.md"):
            (folder / name).write_text("x", encoding="utf-8")

        request = agent_pipeline.build_depth_repair_request(folder, ["最薄章节"])

        assert request["allowed_files"] == [folder / "讲书稿.md"]
        assert folder / "讲书稿.md" not in request["input_files"]
        assert set(request["input_files"]) == {
            folder / "summary_map.json", folder / "content_map.json", folder / "中文完整笔记.md",
        }
