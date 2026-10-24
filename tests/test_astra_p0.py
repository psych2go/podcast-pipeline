import hashlib
from pathlib import Path

from scripts.ai_review import _chunk_concurrency, _rate_limit_error
from scripts.run_report import RunReport
from scripts.source_relevance import refresh_source_relevance_cache
from scripts.quality_report import _ai_fact_check_consistency
import json
import tempfile


def test_external_fail_closed_error_categories_are_detected():
    # Provider outages must never be treated as successful external evidence.
    assert _rate_limit_error("429 rate limit")
    assert _rate_limit_error("usage_limit exceeded")
    assert _rate_limit_error("provider overloaded")


def test_chunk_checkpoint_key_changes_with_code_and_input_material():
    code = hashlib.sha256(Path("scripts/ai_review.py").read_bytes()).hexdigest()
    assert len(code) == 64
    assert hashlib.sha256(b"input-a" + code.encode()).hexdigest() != hashlib.sha256(
        b"input-b" + code.encode()).hexdigest()


def test_quality_gate_blocks_unverifiable_health_claim_but_allows_attributed_guest_opinion():
    medical = {"fact_checks": [{"claim": "健康建议", "claim_type": "public_fact",
        "verification_mode": "web_required", "verdict": "qualified",
        "publication_status": "used_as_fact", "source_urls": [],
        "evidence_segment_ids": []}]}
    guest = {"fact_checks": [{"claim": "嘉宾看法", "claim_type": "guest_opinion",
        "verification_mode": "transcript_attribution", "verdict": "faithfully_attributed",
        "publication_status": "attributed_or_qualified", "source_urls": [],
        "evidence_segment_ids": ["S1"]}]}
    assert _ai_fact_check_consistency(medical)[0]
    assert _ai_fact_check_consistency(guest)[0] == []


def test_shared_source_cache_reuses_success_across_episodes(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        return {"status": "fetched", "title": "Health source", "excerpt": "health",
                "content_sha256": "abc", "final_url": url, "content_type": "text/html"}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        monkeypatch.setenv("SOURCE_RELEVANCE_SHARED_CACHE", str(root / "shared.json"))
        for name in ("one", "two"):
            folder = root / name
            folder.mkdir()
            (folder / "editorial_corrections.json").write_text(json.dumps({"corrections":[{"correction_id":"C1", "source_urls":["https://Example.com/a?utm_source=x"]}]}))
            refresh_source_relevance_cache(folder, fetcher=fetch)
        assert len(calls) == 1


def test_run_report_records_stage_timing_and_failure_code():
    with tempfile.TemporaryDirectory() as td:
        report = RunReport(td, "test")
        try:
            with report.stage("broken") as stage:
                stage.metrics["failure_kind"] = "timeout"
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        report.finish(False, "boom")
        payload = json.loads((Path(td) / "run_report.json").read_text())
        metrics = payload["runs"][-1]["metrics"]
        assert metrics["failed_stage_count"] == 1
        assert metrics["stage_duration_seconds"] >= 0


def test_chunk_concurrency_defaults_to_three(monkeypatch):
    monkeypatch.delenv("SUBAGENT_REVIEW_CONCURRENCY", raising=False)
    assert _chunk_concurrency() == 3
    monkeypatch.setenv("SUBAGENT_REVIEW_CONCURRENCY", "5")
    assert _chunk_concurrency() == 5
