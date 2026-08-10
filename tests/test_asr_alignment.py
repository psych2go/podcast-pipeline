import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_alignment import align_segments  # noqa: E402


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.source = [{
            "start": 0.0,
            "end": 2.0,
            "text": "Example Corp grew quickly.",
            "avg_logprob": -0.2,
            "words": [],
        }]

    def test_alignment_updates_words_without_changing_text(self):
        def runner(_request):
            return {
                "segments": [{
                    "source_index": 0,
                    "start": 0.1,
                    "end": 1.8,
                    "text": "Example Corp grew quickly.",
                    "words": [
                        {
                            "word": "Example",
                            "start": 0.1,
                            "end": 0.5,
                            "score": 0.9,
                        },
                        {
                            "word": "Corp",
                            "start": 0.5,
                            "end": 0.8,
                            "score": 0.95,
                        },
                        {
                            "word": "grew",
                            "start": 0.9,
                            "end": 1.2,
                            "score": 0.92,
                        },
                        {
                            "word": "quickly.",
                            "start": 1.2,
                            "end": 1.8,
                            "score": 0.91,
                        },
                    ],
                }],
                "meta": {
                    "model": "WAV2VEC2_ASR_BASE_960H",
                    "device": "cpu",
                    "elapsed_seconds": 1.2,
                },
            }

        result = align_segments(
            "fake.wav",
            self.source,
            mode="whisperx",
            runner=runner,
        )
        segment = result["segments"][0]
        self.assertEqual(segment["text"], self.source[0]["text"])
        self.assertEqual(segment["start"], 0.1)
        self.assertEqual(segment["end"], 1.8)
        self.assertEqual(segment["words"][0]["probability"], 0.9)
        self.assertEqual(result["meta"]["word_timestamp_coverage"], 1.0)

    def test_alignment_content_drift_is_rejected(self):
        def runner(_request):
            return {
                "segments": [{
                    "source_index": 0,
                    "start": 0,
                    "end": 2,
                    "text": "Completely different words here.",
                    "words": [],
                }],
                "meta": {},
            }

        with self.assertRaisesRegex(RuntimeError, "改变了转录词序"):
            align_segments(
                "fake.wav",
                self.source,
                mode="whisperx",
                runner=runner,
            )

    def test_auto_mode_falls_back_when_runner_fails(self):
        def runner(_request):
            raise RuntimeError("model unavailable")

        result = align_segments(
            "fake.wav",
            self.source,
            mode="auto",
            runner=runner,
        )
        self.assertEqual(result["segments"], self.source)
        self.assertEqual(result["meta"]["status"], "failed")
        self.assertEqual(result["meta"]["warning"], "alignment_failed")


if __name__ == "__main__":
    unittest.main()
