import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fetcher import _extract_podscripts_segments


HTML = (
    '<style>.pagination a{display:none}</style>'
    '<div class="psb-ep__paragraph"><span class="psb-ep__time" data-seconds="0">00:00</span>'
    '<!--[--><span class="psb-ep__sentence" id="segment-0" data-seconds="0"><!--[-->Hello there.'
    '<!--]--></span><span class="psb-ep__sentence" data-seconds="3">It&#39;s a test.</span></div>'
    '<div class="psb-ep__paragraph"><span class="psb-ep__time" data-seconds="58">00:58</span>'
    '<span class="psb-ep__sentence" data-seconds="58">Second <br>paragraph.</span></div>'
)


class PodscriptsPsbMarkupTests(unittest.TestCase):
    def test_new_paragraph_markup_extracts_timestamped_segments(self):
        segments = _extract_podscripts_segments(HTML)
        self.assertEqual([s["start"] for s in segments], [0, 58])
        self.assertEqual(segments[0]["text"], "Hello there. It's a test.")
        self.assertEqual(segments[1]["text"], "Second paragraph.")
        self.assertEqual(segments[0]["end"], 58)


if __name__ == "__main__":
    unittest.main()
