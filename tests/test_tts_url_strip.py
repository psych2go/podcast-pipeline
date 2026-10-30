import unittest

from scripts.content_finalizer import validate_tts_readiness
from scripts.tts import normalize_for_tts


class TtsUrlStripTest(unittest.TestCase):
    def test_parenthetical_source_urls_are_not_spoken(self):
        text = ("人类约有七百万个嗅觉受体（转录来源：https://podscripts.co/podcasts/"
                "huberman-lab/essentials-how-smells）。"
                "补充剂需谨慎（见 www.nccih.nih.gov/health/tips）。")
        spoken = normalize_for_tts(text)
        self.assertNotIn("http", spoken)
        self.assertNotIn("www", spoken)
        self.assertEqual(validate_tts_readiness(text, {}), [])

    def test_plain_parentheses_kept(self):
        self.assertIn("（嗅球）", normalize_for_tts("气味进入（嗅球）。"))


if __name__ == "__main__":
    unittest.main()
