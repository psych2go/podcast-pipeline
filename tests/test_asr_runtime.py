import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_runtime import (  # noqa: E402
    RuntimeSpec,
    benchmark_transcription,
    resolve_runtime,
    resolve_runtime_from_report,
)


class RuntimeSpecTests(unittest.TestCase):
    def test_parse_runtime_spec(self):
        self.assertEqual(
            RuntimeSpec.parse("cuda:int8_float16"),
            RuntimeSpec("cuda", "int8_float16"),
        )

    def test_invalid_runtime_spec_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "device:compute_type"):
            RuntimeSpec.parse("cuda")
        with self.assertRaisesRegex(ValueError, "不支持"):
            RuntimeSpec.parse("metal:float16")

    def test_automatic_runtime_prefers_ready_cuda(self):
        report = {
            "cuda_ready": True,
            "ctranslate2": {
                "cuda_compute_types": ["int8_float16"],
            },
        }
        self.assertEqual(
            resolve_runtime_from_report(report),
            RuntimeSpec("cuda", "int8_float16"),
        )

    def test_automatic_runtime_falls_back_to_cpu(self):
        report = {
            "cuda_ready": False,
            "ctranslate2": {
                "cpu_compute_types": ["int8"],
            },
        }
        self.assertEqual(
            resolve_runtime_from_report(report),
            RuntimeSpec("cpu", "int8"),
        )

    def test_explicit_unsupported_compute_type_is_rejected(self):
        with patch(
                "ctranslate2.get_supported_compute_types",
                return_value={"int8"}):
            with self.assertRaisesRegex(RuntimeError, "不支持"):
                resolve_runtime("cpu", "float16")


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_materializes_segments_and_reports_realtime_factor(self):
        segment = SimpleNamespace(text=" hello benchmark")
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            def transcribe(self, _audio, **kwargs):
                self.kwargs = kwargs
                return iter([segment]), info

        created = []

        def factory(model_size, device, compute_type):
            created.append((model_size, device, compute_type))
            return FakeModel()

        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "clip.wav"
            audio.write_bytes(b"fake")
            with patch(
                    "asr_runtime._audio_duration_seconds",
                    return_value=10.0):
                result = benchmark_transcription(
                    audio,
                    "tiny.en",
                    RuntimeSpec("cpu", "int8"),
                    beam_size=3,
                    model_factory=factory,
                )

        self.assertEqual(created, [("tiny.en", "cpu", "int8")])
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(result["transcript_chars"], len("hello benchmark"))
        self.assertEqual(result["beam_size"], 3)
        self.assertIsNotNone(result["realtime_factor"])
        self.assertEqual(len(result["transcript_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
