"""Long-transcript content-map windowing and degenerate-map rejection."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import agent_pipeline
from scripts.content_map import (
    degenerate_content_map_errors, init_content_map, load_json, save_json)


def _raw(n, chars=1000):
    return {"segments": [
        {"id": f"S{i:04d}", "start": i * 10, "end": i * 10 + 9,
         "text": ("word " * (chars // 5)).strip()}
        for i in range(1, n + 1)]}


class WindowTests(unittest.TestCase):
    def test_windows_are_contiguous_and_skip_empty(self):
        raw = _raw(10)
        raw["segments"][3]["text"] = "  "
        windows = agent_pipeline.content_map_windows(raw["segments"], 2500)
        ids = [s["id"] for w in windows for s in w]
        self.assertNotIn("S0004", ids)
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(len(w) <= 3 for w in windows))

    def test_merge_renumbers_units(self):
        merged = agent_pipeline.merge_content_map_windows(
            {"schema_version": 3},
            [[{"id": "U0001", "claim_evidence": {"C01": ["S0001"]}}],
             [{"id": "U0001"}, {"id": "U0007"}]])
        self.assertEqual([u["id"] for u in merged["units"]],
                         ["U0001", "U0002", "U0003"])
        self.assertEqual(merged["units"][0]["claim_evidence"], {})
        self.assertEqual(merged["schema_version"], 3)

    def test_long_transcript_runs_one_task_per_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save_json(folder / "transcript.raw.json", _raw(9))
            init_content_map(folder / "transcript.raw.json",
                             folder / "content_map.json", title="t")
            calls = []

            def fake_edit(sub, task, *, task_name, allowed_files, **_kw):
                calls.append(task_name)
                path = Path(allowed_files[0])
                payload = load_json(path)
                segs = [s["id"] for s in load_json(
                    Path(sub) / "transcript.raw.json")["segments"]]
                payload["units"] = [{
                    "id": "U0001", "topic": "x", "claims": ["c"],
                    "claim_modalities": ["opinion"], "status": "included",
                    "importance": "high",
                    "evidence": {"mode": "timestamp", "segment_ids": segs}}]
                save_json(path, payload)

            with patch.dict(os.environ, {"CONTENT_MAP_CHUNK_CHARS": "5000",
                                         "CONTENT_MAP_WINDOW_CHARS": "3000"}), \
                    patch.object(agent_pipeline, "run_edit_task", fake_edit):
                agent_pipeline.run_content_map_task(folder, title="t", map_inputs=[])
            merged = load_json(folder / "content_map.json")
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(c.startswith("content_map_") for c in calls))
            self.assertEqual([u["id"] for u in merged["units"]],
                             ["U0001", "U0002", "U0003"])
            covered = [s for u in merged["units"] for s in u["evidence"]["segment_ids"]]
            self.assertEqual(covered, [f"S{i:04d}" for i in range(1, 10)])

    def test_short_transcript_uses_single_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save_json(folder / "transcript.raw.json", _raw(2))
            calls = []
            with patch.object(agent_pipeline, "run_edit_task",
                              lambda *a, **k: calls.append(k["task_name"])):
                agent_pipeline.run_content_map_task(folder, title="t", map_inputs=[])
            self.assertEqual(calls, ["content_map"])


class DegenerateMapTests(unittest.TestCase):
    def test_single_catch_all_unit_is_rejected(self):
        raw = _raw(30)
        ids = [s["id"] for s in raw["segments"]]
        payload = {"units": [{"id": "U0001", "status": "condensed", "claims": [],
                              "evidence": {"segment_ids": ids}}]}
        errors = degenerate_content_map_errors(payload, raw)
        self.assertTrue(any("单个单元" in e for e in errors))
        self.assertTrue(any("没有任何 claim" in e for e in errors))

    def test_split_map_with_claims_passes(self):
        raw = _raw(30)
        segs = raw["segments"]
        units = [{"id": f"U{i:04d}", "status": "included", "claims": ["c"],
                  "evidence": {"segment_ids": [s["id"] for s in segs[i * 10:(i + 1) * 10]]}}
                 for i in range(3)]
        self.assertEqual(degenerate_content_map_errors({"units": units}, raw), [])

    def test_short_transcript_exempt(self):
        raw = _raw(3)
        payload = {"units": [{"id": "U0001", "status": "condensed", "claims": [],
                              "evidence": {"segment_ids": ["S0001", "S0002", "S0003"]}}]}
        self.assertEqual(degenerate_content_map_errors(payload, raw), [])


if __name__ == "__main__":
    unittest.main()
