import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_refinement import (  # noqa: E402
    assess_segment,
    build_asr_context,
    build_refinement_ranges,
    refine_segments,
)


def _segment(
        text,
        start=0.0,
        end=2.0,
        logprob=-0.2,
        compression=1.1,
        no_speech=0.0,
        probability=0.9):
    words = []
    tokens = text.split()
    duration = max(0.01, end - start)
    for index, token in enumerate(tokens):
        word_start = start + duration * index / max(1, len(tokens))
        word_end = start + duration * (index + 1) / max(1, len(tokens))
        words.append({
            "word": " " + token,
            "start": word_start,
            "end": word_end,
            "probability": probability,
        })
    return {
        "start": start,
        "end": end,
        "text": text,
        "avg_logprob": logprob,
        "compression_ratio": compression,
        "no_speech_prob": no_speech,
        "temperature": 0.0,
        "words": words,
    }


class AsrContextTests(unittest.TestCase):
    def test_context_prioritizes_manual_terms_and_deduplicates(self):
        context = build_asr_context(
            title=(
                "Former Intel CEO on What Went Wrong, What's Next "
                "+ Lovable CEO on Vibe Coding"
            ),
            context_texts=["Guest: Pat Gelsinger. Product: Claude Code."],
            hotwords="Claude Code, Intel, Claude Code",
        )
        self.assertEqual(context.terms[0], "Claude Code")
        self.assertEqual(len([
            term for term in context.terms
            if term.lower() == "claude code"
        ]), 1)
        self.assertIn("Intel", context.hotwords)
        self.assertIn("Known names and terms:", context.initial_prompt)
        self.assertIn("manual_hotwords", context.sources)
        self.assertIn("title", context.sources)

    def test_context_is_bounded(self):
        context = build_asr_context(
            title="Alpha Beta",
            hotwords=", ".join(f"Term{i}" for i in range(100)),
            max_terms=5,
            max_hotword_chars=40,
        )
        self.assertLessEqual(len(context.terms), 5)
        self.assertLessEqual(len(context.hotwords), 40)


class SegmentAssessmentTests(unittest.TestCase):
    def test_low_confidence_number_is_selected(self):
        assessment = assess_segment(
            _segment(
                "Revenue reached $15 billion",
                logprob=-1.2,
                probability=0.45,
            ),
            context_terms=("Example Corp",),
        )
        self.assertTrue(assessment.needs_redecode)
        self.assertIn("critical_content", assessment.reasons)
        self.assertIn("low_logprob", assessment.reasons)

    def test_clean_high_confidence_segment_is_not_selected(self):
        assessment = assess_segment(
            _segment("A clear ordinary sentence"),
        )
        self.assertFalse(assessment.needs_redecode)
        self.assertEqual(assessment.reasons, ())

    def test_nearby_difficult_segments_are_merged(self):
        segments = [
            _segment("bad one", 0, 2, logprob=-2),
            _segment("bridge", 2, 3),
            _segment("bad two", 3, 5, logprob=-2),
        ]
        assessments = [
            assess_segment(segment, index)
            for index, segment in enumerate(segments)
        ]
        ranges, truncated = build_refinement_ranges(
            segments, assessments, max_ranges=4)
        self.assertEqual(truncated, 0)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start_index, 0)
        self.assertEqual(ranges[0].end_index, 2)


class AdaptiveRefinementTests(unittest.TestCase):
    def setUp(self):
        self.context = build_asr_context(
            title="Example Corp Revenue",
            hotwords="Example Corp",
        )

    def test_better_candidate_replaces_original_and_is_audited(self):
        original = [
            _segment(
                "Example crop made fifteen billion",
                10,
                14,
                logprob=-2.0,
                compression=2.6,
                probability=0.3,
            ),
        ]

        def decode(start, end, context):
            self.assertEqual((start, end), (10.0, 14.0))
            self.assertIn("Example Corp", context.terms)
            return [
                _segment(
                    "Example Corp made $15 billion",
                    10,
                    14,
                    logprob=-0.2,
                    probability=0.95,
                ),
            ]

        result = refine_segments(original, decode, self.context)
        self.assertEqual(
            result["segments"][0]["text"],
            "Example Corp made $15 billion",
        )
        self.assertEqual(
            result["segments"][0]["refinement"]["kind"],
            "adaptive_redecode",
        )
        self.assertEqual(result["meta"]["accepted_ranges"], 1)
        self.assertEqual(
            result["meta"]["attempts"][0]["decision"],
            "quality_improved",
        )

    def test_implausibly_short_candidate_is_rejected(self):
        original = [
            _segment(
                "A long uncertain sentence containing several important words",
                logprob=-2.0,
                probability=0.3,
            ),
        ]

        result = refine_segments(
            original,
            lambda _start, _end, _context: [
                _segment("short", logprob=-0.1),
            ],
            self.context,
        )
        self.assertEqual(result["segments"][0]["text"], original[0]["text"])
        self.assertEqual(result["segments"][0]["refinement_status"], "rejected")
        self.assertEqual(
            result["meta"]["attempts"][0]["decision"],
            "implausible_length",
        )

    def test_decoder_failure_keeps_original(self):
        original = [_segment("uncertain words here", logprob=-2.0)]

        def fail(_start, _end, _context):
            raise RuntimeError("decoder unavailable")

        result = refine_segments(original, fail, self.context)
        self.assertEqual(result["segments"][0]["text"], original[0]["text"])
        self.assertEqual(result["segments"][0]["refinement_status"], "error")
        self.assertEqual(result["meta"]["failed_ranges"], 1)
        self.assertIn(
            "RuntimeError",
            result["meta"]["attempts"][0]["error"],
        )

    def test_high_confidence_but_divergent_candidate_is_rejected(self):
        original = [
            _segment(
                "Example Corp revenue reached fifteen billion dollars",
                logprob=-2.0,
                probability=0.3,
            ),
        ]
        result = refine_segments(
            original,
            lambda _start, _end, _context: [
                _segment(
                    "The weather tomorrow will be sunny across California",
                    logprob=-0.1,
                    probability=0.99,
                ),
            ],
            self.context,
        )
        self.assertEqual(result["segments"][0]["text"], original[0]["text"])
        self.assertEqual(
            result["meta"]["attempts"][0]["decision"],
            "divergent_candidate",
        )

    def test_prompt_echo_is_replaced_by_high_quality_divergent_candidate(self):
        context = build_asr_context(
            title="AMI ES2004a multi-speaker meeting clip",
            hotwords="T Rex, crocodile, vampire bat, eagle, seagull",
        )
        original = [
            _segment(
                "ES2004a, AMI ES2004a, AMI ES2004a, AMI",
                start=80.0,
                end=94.0,
                logprob=-1.4,
                compression=3.2,
                probability=0.25,
            ),
        ]
        candidate_text = (
            "I'm very impressed with your artistic skills. "
            "Mine are dreadful. This is now coming apart."
        )
        result = refine_segments(
            original,
            lambda _start, _end, _context: [
                _segment(
                    candidate_text,
                    start=80.0,
                    end=94.0,
                    logprob=-0.15,
                    probability=0.96,
                ),
            ],
            context,
        )
        self.assertEqual(result["segments"][0]["text"], candidate_text)
        self.assertEqual(result["meta"]["accepted_ranges"], 1)
        self.assertEqual(
            result["meta"]["attempts"][0]["decision"],
            "prompt_echo_recovered",
        )


if __name__ == "__main__":
    unittest.main()
