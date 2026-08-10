import io
import sys
import unittest
from pathlib import Path
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from prepare_ami_benchmark import (  # noqa: E402
    meeting_speakers,
    meeting_words,
    reference_segments,
)


class AmiReferenceTests(unittest.TestCase):
    def _archive(self):
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(
                "corpusResources/meetings.xml",
                """<?xml version="1.0"?>
                <nite:root xmlns:nite="http://nite.sourceforge.net/">
                  <meeting observation="ES2004a">
                    <speaker nxt_agent="A" global_name="SPK_A"
                             role="PM" channel="0"/>
                  </meeting>
                </nite:root>""",
            )
            archive.writestr(
                "words/ES2004a.A.words.xml",
                """<?xml version="1.0"?>
                <nite:root xmlns:nite="http://nite.sourceforge.net/">
                  <w nite:id="w1" starttime="10.0"
                     endtime="10.5">Hello</w>
                  <w nite:id="w2" starttime="10.5"
                     endtime="10.5" punc="true">.</w>
                  <vocalsound nite:id="v1" starttime="10.6"
                              endtime="10.8"/>
                </nite:root>""",
            )
        buffer.seek(0)
        return ZipFile(buffer)

    def test_nxt_words_and_speaker_mapping(self):
        with self._archive() as archive:
            speakers = meeting_speakers(archive, "ES2004a")
            word_items = meeting_words(
                archive, "ES2004a", speakers)
        self.assertEqual(speakers["A"]["global_name"], "SPK_A")
        self.assertEqual(len(word_items), 1)
        self.assertEqual(word_items[0]["word"], "Hello")

    def test_reference_segments_shift_word_timestamps(self):
        turns = [{
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPK_A",
        }]
        word_items = [{
            "speaker": "SPK_A",
            "start": 10.0,
            "end": 10.5,
            "word": "Hello",
        }]
        segments, stats = reference_segments(
            turns,
            word_items,
            start=10.0,
            end=11.0,
            uri="sample",
        )
        self.assertEqual(segments[0]["words"][0]["start"], 0.0)
        self.assertEqual(stats["assignment_coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
