import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import agent_pipeline as ap


class TtsLexiconRoundsTests(unittest.TestCase):
    def test_second_round_fixes_partial_hyphen_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = folder / "讲书稿.md"
            briefing.write_text("他们讨论 alpha-GPC 和咖啡因。\n", encoding="utf-8")
            lex = folder / "tts_lexicon.json"
            lex.write_text("{}", encoding="utf-8")
            prompts = []

            def fake_edit(_folder, prompt, **_kwargs):
                prompts.append(prompt)
                if len(prompts) == 1:
                    data = {"GPC": "G P C"}
                else:
                    data = {"alpha-GPC": "阿尔法 G P C"}
                lex.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            with patch.object(ap, "run_edit_task", side_effect=fake_edit):
                result = ap._ensure_tts_lexicon_ready(folder, briefing)
            self.assertTrue(result["repaired"])
            self.assertEqual(len(prompts), 2)
            self.assertIn("alpha-GPC", prompts[1])


if __name__ == "__main__":
    unittest.main()
