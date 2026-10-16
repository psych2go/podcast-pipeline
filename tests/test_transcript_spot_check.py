import json
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from scripts import agent_pipeline
from scripts.quality_report import build_quality_report
from scripts.transcript_spot_check import spot_check_transcript


class SpotCheckTests(unittest.TestCase):
    def fixture(self, folder, text=None):
        text = (
            text
            if text is not None
            else "Speaker 1: In 2024 USD 50000 grew 20%. Reliable discussion. " * 40
        )
        raw = {"meta": {"source_kind": "web_transcript"}, "segments": [{"id": "s1", "text": text}]}
        (folder / "原始转录.txt").write_text(text, encoding="utf-8")
        (folder / "transcript.raw.json").write_text(json.dumps(raw), encoding="utf-8")
        return raw

    def test_clean_sample_is_deterministic_and_private_artifact_written(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = self.fixture(folder)
            result = spot_check_transcript(folder, raw)
            self.assertEqual(result, spot_check_transcript(folder, raw))
            self.assertTrue(result["passed"])
            self.assertEqual(result["verification_scope"], "sample_only")
            self.assertIs(result["human_audio"], False)
            self.assertIs(result["full_correction"], False)
            self.assertIn("amount", result["metrics"]["finding_counts"])
            self.assertEqual(
                json.loads((folder / "transcript_spot_check.json").read_text()), result
            )

    def test_risks_escalate(self):
        for text in ("", "short", "transcript unavailable " * 100, "lorem ipsum " * 100):
            with self.subTest(text=text[:30]), tempfile.TemporaryDirectory() as td:
                folder = Path(td)
                result = spot_check_transcript(folder, self.fixture(folder, text))
                self.assertFalse(result["passed"])
                self.assertTrue(result["escalate_to_full_correction"])

    def test_duration_and_config_escalation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = self.fixture(folder)
            raw["meta"]["duration_seconds"] = 3600
            raw["segments"][0].update(start=0, end=10)
            self.assertIn(
                "too_few_timestamped_segments_for_duration",
                spot_check_transcript(folder, raw)["escalation_reasons"],
            )
            for name, value in (
                ("WEB_TRANSCRIPT_SPOT_CHECK", False),
                ("WEB_TRANSCRIPT_REQUIRE_FULL_CORRECTION", True),
            ):
                with mock.patch("scripts.transcript_spot_check.config." + name, value):
                    self.assertTrue(
                        spot_check_transcript(folder, self.fixture(folder))[
                            "escalate_to_full_correction"
                        ]
                    )

    def test_orchestration_pass_escalate_force_and_skip(self):
        for text, force, accepted, expected_calls in (
            (None, False, False, 1),
            ("", False, False, 1),
            (None, True, True, 1),
            (None, False, True, 0),
        ):
            with (
                self.subTest(force=force, accepted=accepted, text=text),
                tempfile.TemporaryDirectory() as td,
            ):
                folder = Path(td)
                self.fixture(folder, text)
                with ExitStack() as stack:
                    stage = SimpleNamespace(metrics={})
                    stack.enter_context(mock.patch.object(
                        agent_pipeline, "_stage",
                        side_effect=lambda *a, stage=stage: nullcontext(stage)))
                    stack.enter_context(
                        mock.patch.object(
                            agent_pipeline, "effective_source_kind", return_value="web_transcript"
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            agent_pipeline,
                            "quality_metadata",
                            return_value={
                                "transcript_status": "可接受" if accepted else "待纠错",
                                "correction_status": "sample_checked",
                            },
                        )
                    )
                    check = stack.enter_context(
                        mock.patch.object(
                            agent_pipeline, "spot_check_transcript", wraps=spot_check_transcript
                        )
                    )
                    status = stack.enter_context(
                        mock.patch.object(agent_pipeline, "update_transcript_status")
                    )

                    def correct(*args, folder=folder, **kwargs):
                        (folder / "转录_纠错.txt").write_text("corrected", encoding="utf-8")

                    writer = stack.enter_context(
                        mock.patch.object(agent_pipeline, "run_edit_task", side_effect=correct)
                    )
                    stack.enter_context(
                        mock.patch.object(
                            agent_pipeline, "_content_map_is_valid", return_value=False
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            agent_pipeline,
                            "_ensure_content_map",
                            side_effect=RuntimeError("stop after correction"),
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, "stop after correction"):
                        agent_pipeline.run_content_pipeline(folder, "test", force=force)
                    self.assertEqual(check.call_count, expected_calls)
                    if expected_calls:
                        self.assertIs(stage.metrics["human_audio"], False)
                        self.assertEqual(stage.metrics["verification_scope"],
                                         "full_correction" if text == "" else "sample_only")
                        self.assertEqual(stage.metrics["spot_check_passed"], text != "")
                    self.assertEqual(writer.call_count, int(text == ""))
                    if expected_calls:
                        self.assertEqual(
                            status.call_args.args[2],
                            "corrected" if text == "" else "sample_checked",
                        )

    def test_quality_scope_warning(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.fixture(folder)
            agent_pipeline.update_transcript_status(folder, "可接受（仅抽样）", "sample_checked")
            spot_check_transcript(folder, json.loads((folder / "transcript.raw.json").read_text()))
            report = build_quality_report(folder)
            self.assertEqual(report["verification_scope"], "sample_only")
            self.assertTrue(any("not human_audio" in w for w in report["warnings"]))
            self.assertTrue(report["spot_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
