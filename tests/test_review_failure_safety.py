"""Synthetic regressions: no private episode fixtures or provider calls."""
import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest import mock

from scripts import ai_review, subagent
from scripts.claim_taxonomy import normalize_review_fact_checks
from scripts.run_report import RunReport


def review():
    return {"passed": True, "fact_checks": [{
        "claim": "嘉宾解释一种机制", "parent_claim_id": "U0001-C01",
        "subclaim_id": "U0001-C01-F01", "claim_type": "not_applicable",
        "claim_origin": "speaker_reported", "speaker_role": "guest",
        "assertion_type": "explanation", "verification_mode": "web_required",
        "risk_domain": "medical", "verdict": "qualified",
        "publication_status": "attributed_or_qualified",
        "evidence_segment_ids": ["S0001"],
        "source_urls": ["https://example.com/research"],
        "checked_at": "2026-01-01", "notes": "限定表述",
    }]}


class ReviewFailureSafetyTests(unittest.TestCase):
    def test_normalizer_never_changes_verdict(self):
        for origin in ("speaker_reported", "speaker_firsthand"):
            for verdict in ("supported", "unsupported", "contradicted", "uncertain"):
                with self.subTest(origin=origin, verdict=verdict):
                    payload = review()
                    item = payload["fact_checks"][0]
                    item.update(claim_origin=origin, assertion_type="fact", verdict=verdict)
                    normalize_review_fact_checks(payload)
                    self.assertEqual(item["verdict"], verdict)

    def test_mechanical_retry_cannot_change_semantic_fields(self):
        changes = {"verdict": "supported", "publication_status": "excluded",
                   "source_urls": [], "notes": "替换结论", "checked_at": "2027-01-01"}
        with tempfile.TemporaryDirectory() as td:
            for field, value in changes.items():
                with self.subTest(field=field):
                    original = review()
                    corrected = copy.deepcopy(original)
                    corrected["fact_checks"][0].update(
                        verification_mode="web_spot_check", **{field: value})
                    with mock.patch.object(ai_review, "run_json_task", return_value={
                            "payload": corrected}) as runner:
                        with self.assertRaisesRegex(ai_review.ReviewContractError, "语义结论"):
                            ai_review._validate_or_retry_review(Path(td), original,
                                                               model="test", effort="max")
                    self.assertFalse(runner.call_args.kwargs["enable_search"])

    def _run_contract_failure(self, folder, corrected, *, write_error=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(ai_review, "REVIEW_FILES", ()))
        stack.enter_context(mock.patch.object(ai_review, "_prompt", return_value="test review"))
        for name, value in (("reviewed_hashes", {"content_map.json": "abc"}),
                            ("review_context_hashes", {}), ("review_scope", {})):
            stack.enter_context(mock.patch.object(ai_review, name, return_value=value))
        for name in ("refresh_source_relevance_cache", "_write_cache_review_context"):
            stack.enter_context(mock.patch.object(ai_review, name))
        stack.enter_context(mock.patch.object(ai_review, "isolated_review_workspace",
                                              return_value=nullcontext(folder)))
        result = {"payload": review(), "model": "test", "task_name": "ai_review",
                  "duration_ms": 10, "retry_count": 0}
        retry = corrected if isinstance(corrected, Exception) else {"payload": corrected}
        stack.enter_context(mock.patch.object(ai_review, "run_json_task",
                                              side_effect=[result, retry]))
        if write_error:
            stack.enter_context(mock.patch.object(ai_review, "atomic_write_json",
                                                  side_effect=OSError("disk full")))
        return ai_review.review_episode(folder, model="test")

    def test_contract_failure_snapshot_links_stage_without_publishing_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with self.assertRaises(ai_review.ReviewContractError):
                self._run_contract_failure(folder, review())
            report = json.loads((folder / "run_report.json").read_text())
            stage = report["runs"][-1]["stages"][-1]
            self.assertEqual(stage["status"], "failed")
            path = folder / stage["metrics"]["contract_failure_snapshot"]
            self.assertEqual(path.stem, stage["id"])
            data = json.loads(path.read_text())
            self.assertFalse(data["authoritative"])
            self.assertEqual(data["original_output"], review())
            self.assertEqual(data["corrected_output"], review())
            self.assertTrue(data["audit"]["initial_errors"])
            self.assertTrue(data["audit"]["final_errors"])
            self.assertEqual(data["initial_call"]["duration_ms"], 10)
            for name in ("ai_review.json", "fact_check_cache.json", "来源.md", "episode.json"):
                self.assertFalse((folder / name).exists())

    def test_contract_diagnostics_are_not_review_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            diagnostics = folder / "ai_review_contract_failures"
            diagnostics.mkdir()
            (diagnostics / "failure.json").write_text('{"original_output": "private"}')
            snapshot = ai_review.reviewed_hashes(folder)
            context = ai_review.review_context_hashes(folder)
            self.assertFalse(any("contract_failures" in key for key in (*snapshot, *context)))
            with ai_review.isolated_review_workspace(folder, snapshot, context) as workspace:
                self.assertFalse((workspace / "ai_review_contract_failures").exists())

    def test_retry_runner_failure_retains_original_and_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            error = subagent.SubagentError("rate_limit", metrics={"failure_kind": "rate_limit"})
            with self.assertRaises(ai_review.ReviewContractError):
                self._run_contract_failure(folder, error)
            data = json.loads(next(folder.glob("ai_review_contract_failures/*.json")).read_text())
            self.assertEqual(data["original_output"], review())
            self.assertIsNone(data["corrected_output"])
            self.assertEqual(data["retry_failure"]["failure_kind"], "rate_limit")

    def test_snapshot_write_failure_stays_failed(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with self.assertRaisesRegex(OSError, "disk full"):
                self._run_contract_failure(folder, review(), write_error=True)
            self.assertFalse((folder / "ai_review.json").exists())
            report = json.loads((folder / "run_report.json").read_text())
            self.assertEqual(report["runs"][-1]["status"], "failed")


class RunnerFailureTests(unittest.TestCase):
    def test_error_summary_does_not_echo_prompt_or_secret(self):
        text = ('prompt: 429 examples secret-token\n'
                'ERROR: exceeded retry limit, last status: 429 Too Many Requests')
        self.assertEqual(subagent._runner_failure(text, ""), ("rate_limit", 429))
        self.assertEqual(subagent._runner_failure(
            "progress", '{"error":{"code":"model_not_found"}}'), ("model_not_found", None))
        self.assertEqual(subagent._runner_failure(
            '{"error":{"code":{}}}', ""), ("unknown", None))
        self.assertEqual(subagent._runner_failure("正文包含 429 和 model_not_found", ""),
                         ("unknown", None))

    def test_failures_record_attempts_and_stop_on_configuration_error(self):
        for stderr, expected, attempts in (
                ('{"error":{"code":"model_not_found"}}', "model_not_found", 1),
                ("ERROR: last status: 401 Unauthorized", "authentication", 1),
                ("private prompt\nERROR: last status: 429 Too Many Requests", "rate_limit", 3)):
            with self.subTest(kind=expected), tempfile.TemporaryDirectory() as td:
                folder = Path(td)
                with mock.patch.dict("os.environ", {"SUBAGENT_MAX_RETRIES": "2"}), \
                        mock.patch.object(subagent, "_runner_commands",
                                          return_value=[["codex", "exec"]]), \
                        mock.patch.object(subagent, "_runner_environment", return_value={}), \
                        mock.patch.object(subagent.time, "sleep"), \
                        mock.patch.object(subagent, "_run_process", return_value=
                                          subprocess.CompletedProcess([], 1, "", stderr)) as runner:
                    report = RunReport(folder, "test")
                    with self.assertRaises(subagent.SubagentError) as caught, report.stage("test"):
                        subagent._run(folder, "private prompt", task_name="test", model="test")
                self.assertEqual(runner.call_count, attempts)
                self.assertNotIn("private prompt", str(caught.exception))
                metrics = caught.exception.failure_metrics
                self.assertEqual(metrics["attempt_count"], attempts)
                self.assertEqual(metrics["failure_kind"], expected)
                saved = json.loads((folder / "run_report.json").read_text())
                self.assertEqual(
                    saved["runs"][-1]["stages"][-1]["metrics"]["runner_failure"], metrics)


if __name__ == "__main__":
    unittest.main()
