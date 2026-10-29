import unittest

from scripts.canonical_entities import prune_unknown_entity_segments


TRANSCRIPT = {"segments": [
    {"id": "S0001", "text": "Ross Edgley swam around Great Britain."},
    {"id": "S0002", "text": "Andrew talks about motivation."},
]}


class EntitySegmentPruningTest(unittest.TestCase):
    def test_drops_unknown_ids_and_keeps_known(self):
        payload = {"entities": [{"observed_names": ["Ross"],
                                 "segment_ids": ["S0001", "S2415"]}]}
        self.assertEqual(prune_unknown_entity_segments(payload, TRANSCRIPT), 1)
        self.assertEqual(payload["entities"][0]["segment_ids"], ["S0001"])

    def test_reanchors_by_observed_name_when_all_unknown(self):
        payload = {"entities": [{"observed_names": ["Great Britain"],
                                 "segment_ids": ["S1022"]}]}
        prune_unknown_entity_segments(payload, TRANSCRIPT)
        self.assertEqual(payload["entities"][0]["segment_ids"], ["S0001"])

    def test_unanchorable_entity_stays_empty_and_fails_closed(self):
        payload = {"entities": [{"observed_names": ["Nobody"],
                                 "segment_ids": ["S0637"]}]}
        prune_unknown_entity_segments(payload, TRANSCRIPT)
        self.assertEqual(payload["entities"][0]["segment_ids"], [])


if __name__ == "__main__":
    unittest.main()
