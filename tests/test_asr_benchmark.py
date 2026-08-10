import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_benchmark import (  # noqa: E402
    benchmark_sample,
    cp_word_error_rate,
    diarization_metrics,
    lexical_metrics,
    read_rttm,
    speaker_attributed_wer,
    word_timestamp_metrics,
)


class DiarizationMetricTests(unittest.TestCase):
    def test_exact_turns_have_zero_der_and_jer(self):
        turns = [
            {"start": 0.0, "end": 1.0, "speaker": "A"},
            {"start": 1.0, "end": 2.0, "speaker": "B"},
        ]
        result = diarization_metrics(turns, turns)
        self.assertEqual(result["der"], 0.0)
        self.assertEqual(result["jer"], 0.0)

    def test_rttm_parser(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.rttm"
            path.write_text(
                "SPEAKER sample 1 1.5 2.0 <NA> <NA> SPK1 <NA> <NA>\n",
                encoding="utf-8",
            )
            turns = read_rttm(path, uri="sample")
        self.assertEqual(turns[0]["start"], 1.5)
        self.assertEqual(turns[0]["end"], 3.5)
        self.assertEqual(turns[0]["speaker"], "SPK1")

    def test_benchmark_prefers_explicit_diarization_turns(self):
        reference_segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "speaker": "A",
                "text": "hello",
            },
            {
                "start": 1.0,
                "end": 2.0,
                "speaker": "B",
                "text": "world",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reference.json"
            path.write_text(
                json.dumps({"segments": reference_segments}),
                encoding="utf-8",
            )
            result = benchmark_sample(
                {
                    "id": "sample",
                    "reference": {"segments_json": str(path)},
                },
                {
                    "segments": [{
                        "start": 0.0,
                        "end": 2.0,
                        "speaker": "wrong",
                        "text": "hello world",
                    }],
                    "diarization_turns": reference_segments,
                },
            )
        self.assertEqual(result["diarization"]["der"], 0.0)
        self.assertEqual(result["diarization"]["jer"], 0.0)


class SpeakerWerTests(unittest.TestCase):
    def setUp(self):
        self.reference = [
            {"start": 0, "end": 1, "speaker": "A", "text": "hello world"},
            {"start": 1, "end": 2, "speaker": "B", "text": "second speaker"},
        ]

    def test_cpwer_finds_permuted_speakers(self):
        hypothesis = [
            {"start": 0, "end": 1, "speaker": "Y", "text": "hello world"},
            {"start": 1, "end": 2, "speaker": "X", "text": "second speaker"},
        ]
        result = cp_word_error_rate(self.reference, hypothesis)
        self.assertEqual(result["wer"], 0.0)

    def test_speaker_attributed_wer_uses_temporal_mapping(self):
        hypothesis = [
            {"start": 0, "end": 1, "speaker": "Y", "text": "hello world"},
            {"start": 1, "end": 2, "speaker": "X", "text": "second error"},
        ]
        result = speaker_attributed_wer(
            self.reference,
            hypothesis,
            {"Y": "A", "X": "B"},
        )
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["substitutions"], 1)
        self.assertEqual(result["wer"], 0.25)


class LexicalAndTimestampTests(unittest.TestCase):
    def test_entities_numbers_and_timestamp_mae(self):
        reference = [{
            "text": "OpenAI raised $10 billion",
            "words": [
                {"word": "OpenAI", "start": 0.0, "end": 0.5},
                {"word": "raised", "start": 0.5, "end": 1.0},
            ],
        }]
        hypothesis = [{
            "text": "OpenAI raised $10 billion",
            "words": [
                {"word": "OpenAI", "start": 0.1, "end": 0.6},
                {"word": "raised", "start": 0.6, "end": 1.1},
            ],
        }]
        lexical = lexical_metrics(
            reference, hypothesis, entities=["OpenAI"])
        timestamps = word_timestamp_metrics(reference, hypothesis)
        self.assertEqual(lexical["entity_recall"], 1.0)
        self.assertEqual(lexical["number_recall"], 1.0)
        self.assertEqual(timestamps["matched_words"], 2)
        self.assertEqual(timestamps["start_mae_seconds"], 0.1)

    def test_curated_number_targets_match_written_and_digit_forms(self):
        reference = [{"text": "selling price at twenty five Euros"}]
        hypothesis = [{"text": "selling price at 25 euros"}]
        result = lexical_metrics(
            reference,
            hypothesis,
            number_targets=[{
                "name": "25 EUR",
                "variants": ["twenty five euros", "25 euros", "€25"],
            }],
        )
        self.assertEqual(result["number_metric"], "curated_targets")
        self.assertEqual(result["number_recall"], 1.0)
        self.assertEqual(result["number_precision"], 1.0)
        self.assertEqual(
            result["number_targets"][0]["matched_hits"], 1)


if __name__ == "__main__":
    unittest.main()
