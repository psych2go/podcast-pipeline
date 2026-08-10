import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from diarize import _output_turns, diarize_and_merge  # noqa: E402


class FakeAnnotation:
    def __init__(self, turns):
        self.turns = turns

    def itertracks(self, yield_label=False):
        for start, end, label in self.turns:
            yield SimpleNamespace(start=start, end=end), None, label


class DiarizationOutputTests(unittest.TestCase):
    def test_exclusive_output_is_preferred(self):
        output = SimpleNamespace(
            exclusive_speaker_diarization=FakeAnnotation([
                (0.0, 1.0, "SPEAKER_00"),
            ]),
            speaker_diarization=FakeAnnotation([
                (0.0, 0.8, "SPEAKER_00"),
                (0.7, 1.0, "SPEAKER_01"),
            ]),
        )
        turns, exclusive = _output_turns(output, prefer_exclusive=True)
        self.assertTrue(exclusive)
        self.assertEqual(turns, [(0.0, 1.0, "SPEAKER_00")])

    def test_standard_output_is_fallback(self):
        output = SimpleNamespace(
            speaker_diarization=FakeAnnotation([
                (0.0, 1.0, "SPEAKER_01"),
            ]),
        )
        turns, exclusive = _output_turns(output, prefer_exclusive=True)
        self.assertFalse(exclusive)
        self.assertEqual(turns[0][2], "SPEAKER_01")

    def test_diarize_and_merge_can_return_metadata(self):
        segments = [{
            "start": 0.0,
            "end": 1.0,
            "text": "hello",
            "words": [{
                "word": "hello",
                "start": 0.0,
                "end": 1.0,
            }],
        }]
        with patch("diarize.diarize", return_value={
            "turns": [(0.0, 1.0, "SPEAKER_00")],
            "meta": {
                "model": "community-1",
                "exclusive_used": True,
            },
        }):
            result = diarize_and_merge(
                "fake.wav",
                segments,
                return_metadata=True,
            )
        self.assertEqual(
            result["segments"][0]["speaker"], "SPEAKER_00")
        self.assertTrue(result["meta"]["exclusive_used"])


if __name__ == "__main__":
    unittest.main()
