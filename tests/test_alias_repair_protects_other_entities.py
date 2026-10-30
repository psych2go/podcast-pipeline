import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.canonical_entities import repair_public_entity_aliases


class AliasRepairProtectionTests(unittest.TestCase):
    def test_first_name_variant_does_not_rewrite_other_full_name(self):
        payload = {"entities": [
            {"canonical_name": "David Goggins", "observed_names": ["Goggins"],
             "public_aliases": [], "entity_type": "person"},
            {"canonical_name": "David protein bars", "observed_names": ["David"],
             "public_aliases": [], "entity_type": "product"},
        ]}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "讲书稿.md"
            path.write_text("David Goggins 式激励。广告里的 David 很好吃。", encoding="utf-8")
            repair_public_entity_aliases(payload, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("David Goggins 式激励", text)
        self.assertNotIn("David protein bars Goggins", text)
        self.assertIn("广告里的 David protein bars 很好吃", text)
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()


class AliasErrorProtectionTests(unittest.TestCase):
    def test_first_name_inside_other_full_name_is_not_a_leak(self):
        from scripts.canonical_entities import public_entity_alias_errors
        payload = {"entities": [
            {"canonical_name": "David Goggins", "observed_names": ["Goggins"],
             "public_aliases": [], "entity_type": "person"},
            {"canonical_name": "David protein bars", "observed_names": ["David"],
             "public_aliases": [], "entity_type": "product"},
        ]}
        self.assertEqual(public_entity_alias_errors(payload, "David Goggins 式激励"), [])
        self.assertTrue(public_entity_alias_errors(payload, "广告里的 David 很好吃"))
