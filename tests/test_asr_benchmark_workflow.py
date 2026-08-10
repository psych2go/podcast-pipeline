import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asr_benchmark_workflow import (  # noqa: E402
    check_policy_contract,
    rescore_benchmark_workflow,
    run_benchmark_workflow,
    verify_report_payload,
)


def _contract():
    return {
        "reference_contract": {
            "sample_id": "sample",
            "manifest_sha256": "manifest-hash",
            "audio": {
                "bytes": 100,
                "sha256": "audio-hash",
            },
            "files": {
                "reference.json": "reference-hash",
            },
        },
        "required_policy_models": ["large", "turbo"],
        "expected_recommendation": {
            "first_pass_model": "turbo",
            "review_model": "turbo",
        },
        "acceptance": {
            "shared_diarization": {
                "speaker_count": 2,
                "exclusive_used": True,
            },
            "strict_diarization": {
                "max_der": 0.3,
                "max_jer": 0.4,
            },
            "models": {
                "turbo": {
                    "max_cpwer": 0.2,
                    "max_speaker_attributed_wer": 0.2,
                    "min_number_recall": 0.8,
                    "min_entity_recall": 0.7,
                    "min_alignment_coverage": 0.95,
                    "max_start_mae_seconds": 0.3,
                    "max_end_mae_seconds": 0.4,
                },
            },
            "comparisons": [{
                "faster_model": "turbo",
                "reference_model": "large",
                "min_speedup": 1.5,
                "max_cpwer_regression": 0.02,
                "max_speaker_attributed_wer_regression": 0.02,
            }],
        },
    }


def _run(model, wall, wer):
    return {
        "model": model,
        "status": "passed",
        "wall_seconds": wall,
        "asr_meta": {
            "alignment": {"word_timestamp_coverage": 0.99},
        },
        "metrics": {
            "diarization": {"der": 0.2, "jer": 0.3},
            "cpwer": {"wer": wer},
            "speaker_attributed_wer": {"wer": wer},
            "lexical": {
                "number_recall": 0.9,
                "entity_recall": 0.8,
            },
            "timestamps": {
                "start_mae_seconds": 0.2,
                "end_mae_seconds": 0.3,
            },
        },
    }


def _report():
    return {
        "schema_version": 2,
        "sample_id": "sample",
        "inputs": {
            "manifest": {"sha256": "manifest-hash"},
            "audio": {"bytes": 100, "sha256": "audio-hash"},
            "references": {
                "segments_json": {"sha256": "reference-hash"},
            },
        },
        "shared_diarization": {
            "speaker_count": 2,
            "exclusive_used": True,
        },
        "runs": [
            _run("large", 100, 0.19),
            _run("turbo", 50, 0.18),
        ],
        "recommendation": {
            "first_pass_model": "turbo",
            "review_model": "turbo",
        },
    }


class PolicyContractTests(unittest.TestCase):
    def test_repository_contract_matches_production_defaults(self):
        result = check_policy_contract()
        self.assertTrue(result["passed"], result["errors"])

    def test_contract_detects_production_preset_drift(self):
        result = check_policy_contract(preset_models={
            "fast": "medium",
            "balanced": "large-v3",
            "max": "large-v3",
        })
        self.assertFalse(result["passed"])
        self.assertTrue(any(
            "production presets" in error
            for error in result["errors"]
        ))

    def test_report_within_contract_passes(self):
        self.assertEqual(
            verify_report_payload(_contract(), _report()),
            [],
        )

    def test_report_quality_regression_is_blocked(self):
        report = copy.deepcopy(_report())
        report["runs"][1]["metrics"]["cpwer"]["wer"] = 0.4
        errors = verify_report_payload(_contract(), report)
        self.assertTrue(any("turbo cpWER=" in error for error in errors))
        self.assertTrue(any(
            "turbo cpWER regression=" in error
            for error in errors
        ))

    def test_run_interface_prepares_runs_and_writes_verification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"id": "sample"}),
                encoding="utf-8",
            )
            contract_path = root / "policy.json"
            contract_path.write_text(json.dumps({
                "default_manifest": "manifest.json",
                "reference_contract": {
                    "start_seconds": 10.0,
                    "duration_seconds": 20.0,
                },
            }), encoding="utf-8")
            with patch(
                    "asr_benchmark_workflow.prepare") as prepare_mock, \
                    patch(
                        "asr_benchmark_workflow.check_policy_contract",
                        return_value={"passed": True},
                    ), \
                    patch(
                        "asr_benchmark_workflow.run_manifest",
                        return_value={"recommendation": {"status": "ready"}},
                    ) as run_mock, \
                    patch(
                        "asr_benchmark_workflow.verify_report_payload",
                        return_value=[]):
                result = run_benchmark_workflow(
                    contract_path,
                    reuse_shared_diarization=True,
                )
            verification = Path(result["verification"])
            payload = json.loads(
                verification.read_text(encoding="utf-8"))
        prepare_mock.assert_called_once_with(
            manifest_path.parent,
            start=10.0,
            duration=20.0,
        )
        run_mock.assert_called_once_with(
            manifest_path,
            reuse_shared_diarization=True,
        )
        self.assertTrue(payload["passed"])

    def test_rescore_interface_reuses_hypotheses_and_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"id": "sample"}),
                encoding="utf-8",
            )
            contract_path = root / "policy.json"
            contract_path.write_text(json.dumps({
                "default_manifest": "manifest.json",
            }), encoding="utf-8")
            with patch(
                    "asr_benchmark_workflow.check_policy_contract",
                    return_value={"passed": True},
                    ), \
                    patch(
                    "asr_benchmark_workflow.rescore_manifest",
                    return_value={"recommendation": {"status": "ready"}},
                    ) as rescore_mock, \
                    patch(
                        "asr_benchmark_workflow.verify_report_payload",
                        return_value=[]):
                result = rescore_benchmark_workflow(contract_path)
        rescore_mock.assert_called_once_with(manifest_path)
        self.assertTrue(result["passed"])

    def test_run_stops_before_inference_when_contract_drifted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"id": "sample"}),
                encoding="utf-8",
            )
            contract_path = root / "policy.json"
            contract_path.write_text(json.dumps({
                "default_manifest": "manifest.json",
                "reference_contract": {
                    "start_seconds": 10.0,
                    "duration_seconds": 20.0,
                },
            }), encoding="utf-8")
            with patch("asr_benchmark_workflow.prepare"), \
                    patch(
                        "asr_benchmark_workflow.check_policy_contract",
                        return_value={
                            "passed": False,
                            "errors": ["preset drift"],
                            "warnings": [],
                        },
                    ), \
                    patch(
                        "asr_benchmark_workflow.run_manifest") as run_mock:
                result = run_benchmark_workflow(contract_path)
        run_mock.assert_not_called()
        self.assertEqual(result["stage"], "contract_check")


if __name__ == "__main__":
    unittest.main()
