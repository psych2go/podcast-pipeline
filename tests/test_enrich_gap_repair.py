import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from content_map import enrich_content_map_evidence, source_segment_accountability  # noqa: E402


class EnrichGapRepairTest(unittest.TestCase):
    def test_timestamp_gap_is_kept_after_enrichment(self):
        transcript = {"segments": [
            {"id": f"S{i:04d}", "start": float(i), "end": float(i + 1), "text": f"t{i}"}
            for i in range(1, 7)
        ]}
        cm = {"source_accountability_version": 1, "units": [
            {"id": "U0001", "status": "included", "claims": [], "timestamps": [[1.0, 3.0]],
             "evidence": {"mode": "timestamp", "segment_ids": ["S0001", "S0002"]}},
            {"id": "U0002", "status": "included", "claims": [], "timestamps": [[4.0, 7.0]],
             "evidence": {"mode": "timestamp", "segment_ids": ["S0004", "S0005", "S0006"]}},
        ]}
        out, t = enrich_content_map_evidence(copy.deepcopy(cm), transcript)
        self.assertEqual(source_segment_accountability(out, t)["missing_ids"], [])


if __name__ == "__main__":
    unittest.main()
