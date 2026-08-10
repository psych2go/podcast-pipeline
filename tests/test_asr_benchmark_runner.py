import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_benchmark_runner import (  # noqa: E402
    file_fingerprint,
    recommend_model_policy,
    rescore_manifest,
    resolve_manifest_paths,
    run_manifest,
    run_policy,
)


def _run(name, model, wall, cpwer, sawer):
    return {
        "name": name,
        "model": model,
        "status": "passed",
        "wall_seconds": wall,
        "metrics": {
            "cpwer": {"wer": cpwer},
            "speaker_attributed_wer": {"wer": sawer},
        },
    }


class PolicyRecommendationTests(unittest.TestCase):
    def test_fast_policy_is_selected_within_quality_tolerance(self):
        recommendation = recommend_model_policy([
            _run("large", "large-v3", 100, 0.10, 0.12),
            _run("turbo", "large-v3-turbo", 50, 0.11, 0.13),
        ])
        self.assertEqual(
            recommendation["first_pass_policy"], "turbo")
        self.assertEqual(recommendation["review_policy"], "large")
        self.assertEqual(recommendation["speedup_vs_review"], 2.0)

    def test_quality_regression_keeps_best_policy(self):
        recommendation = recommend_model_policy([
            _run("large", "large-v3", 100, 0.10, 0.12),
            _run("turbo", "large-v3-turbo", 40, 0.20, 0.25),
        ])
        self.assertEqual(
            recommendation["first_pass_policy"], "large")


class RunnerTests(unittest.TestCase):
    def test_manifest_paths_are_relative_to_manifest(self):
        manifest = {
            "id": "sample",
            "audio": "audio.wav",
            "reference": {"rttm": "ref.rttm"},
        }
        resolved = resolve_manifest_paths(
            manifest, "/tmp/suite/manifest.json")
        self.assertEqual(
            resolved["audio"], "/tmp/suite/audio.wav")
        self.assertEqual(
            resolved["reference"]["rttm"],
            "/tmp/suite/ref.rttm",
        )

    def test_runner_writes_hypothesis_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "audio.wav").write_bytes(b"audio")
            (root / "reference.json").write_text(
                '{"segments":[]}', encoding="utf-8")
            manifest = {
                "id": "sample",
                "audio": "audio.wav",
                "reference": {
                    "kind": "human",
                    "segments_json": "reference.json",
                },
                "policies": [{
                    "name": "large",
                    "model": "large-v3",
                }],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            fake_result = {
                "segments": [],
                "meta": {"elapsed_seconds": 1},
                "text": "",
            }
            fake_metrics = {
                "cpwer": {"wer": 0.0},
                "speaker_attributed_wer": {"wer": 0.0},
            }
            with patch(
                    "asr_benchmark_runner.transcribe",
                    return_value=fake_result), \
                    patch(
                        "asr_benchmark_runner.benchmark_sample",
                        return_value=fake_metrics):
                report = run_manifest(path)
        self.assertEqual(report["runs"][0]["status"], "passed")
        self.assertEqual(
            report["recommendation"]["review_policy"], "large")

    def test_shared_diarization_turns_are_scored_directly(self):
        shared = {
            "turns": [(0.0, 1.0, "SPEAKER_00")],
            "meta": {
                "model": "community-1",
                "exclusive_used": True,
            },
        }
        fake_result = {
            "segments": [{
                "start": 0.0,
                "end": 1.0,
                "text": "hello",
            }],
            "meta": {},
            "text": "hello",
        }
        observed = {}

        def score(_manifest, payload):
            observed["turns"] = payload.get("diarization_turns")
            return {
                "cpwer": {"wer": 0.0},
                "speaker_attributed_wer": {"wer": 0.0},
            }

        with tempfile.TemporaryDirectory() as td, \
                patch(
                    "asr_benchmark_runner.transcribe",
                    return_value=fake_result), \
                patch(
                    "asr_benchmark_runner.benchmark_sample",
                    side_effect=score):
            run_policy(
                {
                    "id": "sample",
                    "audio": "audio.wav",
                    "reference": {},
                },
                {"name": "large", "model": "large-v3"},
                Path(td),
                shared_diarization=shared,
            )
        self.assertEqual(observed["turns"], [{
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPEAKER_00",
        }])

    def test_runner_reuses_persisted_shared_diarization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "results"
            output.mkdir()
            (root / "audio.wav").write_bytes(b"audio")
            audio_fingerprint = file_fingerprint(root / "audio.wav")
            (root / "reference.json").write_text(
                '{"segments":[]}', encoding="utf-8")
            (output / "shared_diarization.json").write_text(
                json.dumps({
                    "model": "community-1",
                    "speaker_count": 1,
                    "turn_count": 1,
                    "input_audio": audio_fingerprint,
                    "speaker_constraints": {
                        "min_speakers": None,
                        "max_speakers": None,
                    },
                    "turns": [{
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": "SPEAKER_00",
                    }],
                }),
                encoding="utf-8",
            )
            manifest = {
                "id": "sample",
                "audio": "audio.wav",
                "reference": {
                    "kind": "human",
                    "segments_json": "reference.json",
                },
                "shared_diarization": True,
                "policies": [{
                    "name": "large",
                    "model": "large-v3",
                }],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            fake_result = {
                "segments": [{
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                }],
                "meta": {},
                "text": "hello",
            }
            fake_metrics = {
                "cpwer": {"wer": 0.0},
                "speaker_attributed_wer": {"wer": 0.0},
            }
            with patch(
                    "asr_benchmark_runner.transcribe",
                    return_value=fake_result), \
                    patch(
                        "asr_benchmark_runner.benchmark_sample",
                        return_value=fake_metrics), \
                    patch("diarize.diarize") as diarize_mock:
                report = run_manifest(
                    path,
                    output_dir=output,
                    reuse_shared_diarization=True,
                )
        diarize_mock.assert_not_called()
        self.assertTrue(report["shared_diarization"]["reused"])
        self.assertEqual(report["runs"][0]["status"], "passed")
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(
            report["inputs"]["audio"]["sha256"],
            audio_fingerprint["sha256"],
        )

    def test_runner_rejects_shared_diarization_for_other_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.wav"
            audio.write_bytes(b"current")
            cache = root / "shared.json"
            old_audio = root / "old.wav"
            old_audio.write_bytes(b"old")
            cache.write_text(json.dumps({
                "input_audio": file_fingerprint(old_audio),
                "speaker_constraints": {
                    "min_speakers": 2,
                    "max_speakers": 2,
                },
                "turns": [[0.0, 1.0, "SPEAKER_00"]],
            }), encoding="utf-8")
            from asr_benchmark_runner import load_shared_diarization

            with self.assertRaisesRegex(ValueError, "当前音频不匹配"):
                load_shared_diarization(
                    cache,
                    audio_path=audio,
                    min_speakers=2,
                    max_speakers=2,
                )

    def test_rescore_reuses_hypotheses_without_transcribe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "results"
            hypothesis_dir = output / "hypotheses"
            hypothesis_dir.mkdir(parents=True)
            (root / "audio.wav").write_bytes(b"audio")
            (root / "reference.json").write_text(
                '{"segments":[]}', encoding="utf-8")
            manifest = {
                "id": "sample",
                "audio": "audio.wav",
                "reference": {
                    "kind": "human",
                    "segments_json": "reference.json",
                },
                "policies": [{
                    "name": "turbo",
                    "model": "large-v3-turbo",
                }],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            hypothesis = {
                "segments": [],
                "meta": {"model": "large-v3-turbo"},
                "text": "",
            }
            (hypothesis_dir / "turbo.json").write_text(
                json.dumps(hypothesis), encoding="utf-8")
            (output / "report.json").write_text(json.dumps({
                "shared_diarization": {
                    "speaker_count": 1,
                    "reused": False,
                },
                "runs": [{
                    "name": "turbo",
                    "wall_seconds": 12.5,
                }],
            }), encoding="utf-8")
            fake_metrics = {
                "cpwer": {"wer": 0.0},
                "speaker_attributed_wer": {"wer": 0.0},
            }
            with patch(
                    "asr_benchmark_runner.benchmark_sample",
                    return_value=fake_metrics), \
                    patch(
                        "asr_benchmark_runner.transcribe") as transcribe_mock:
                report = rescore_manifest(path, output_dir=output)
        transcribe_mock.assert_not_called()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["runs"][0]["wall_seconds"], 12.5)
        self.assertTrue(report["shared_diarization"]["reused"])
        self.assertTrue(report["shared_diarization"]["rescore_only"])


if __name__ == "__main__":
    unittest.main()
