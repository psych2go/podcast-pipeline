"""Contract-driven entry point for the ASR benchmark workflow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from asr_benchmark_runner import (
        file_fingerprint,
        rescore_manifest,
        run_manifest,
    )
    from atomic_io import atomic_write_json
    from fetcher import preset_model_policy
    from prepare_ami_benchmark import EXPECTED_HASHES, prepare, verify_sources
except ImportError:
    from scripts.asr_benchmark_runner import (
        file_fingerprint,
        rescore_manifest,
        run_manifest,
    )
    from scripts.atomic_io import atomic_write_json
    from scripts.fetcher import preset_model_policy
    from scripts.prepare_ami_benchmark import (
        EXPECTED_HASHES,
        prepare,
        verify_sources,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = PROJECT_ROOT / "benchmarks" / "asr-policy.json"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _contract_context(contract_path):
    contract_path = Path(contract_path).resolve()
    contract = _read_json(contract_path)
    manifest_path = (
        contract_path.parent / contract["default_manifest"]
    ).resolve()
    manifest = _read_json(manifest_path)
    return contract_path, contract, manifest_path, manifest


def _default_report_path(manifest_path, manifest):
    output = manifest.get("output_dir")
    if output:
        path = Path(output)
        if not path.is_absolute():
            path = manifest_path.parent / path
        return path / "report.json"
    return (
        manifest_path.parent
        / "results"
        / manifest["id"]
        / "report.json"
    )


def _append_mismatch(errors, label, actual, expected):
    if actual != expected:
        errors.append(
            f"{label} 不匹配: actual={actual!r} expected={expected!r}")


def _check_upper(errors, label, actual, limit):
    if not isinstance(actual, (int, float)):
        errors.append(f"{label} 缺失或不是数字")
    elif actual > limit:
        errors.append(f"{label}={actual:.4f} 超过上限 {limit:.4f}")


def _check_lower(errors, label, actual, limit):
    if not isinstance(actual, (int, float)):
        errors.append(f"{label} 缺失或不是数字")
    elif actual < limit:
        errors.append(f"{label}={actual:.4f} 低于下限 {limit:.4f}")


def check_policy_contract(contract_path=DEFAULT_CONTRACT, preset_models=None):
    errors = []
    warnings = []
    try:
        (
            contract_path,
            contract,
            manifest_path,
            manifest,
        ) = _contract_context(contract_path)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        return {
            "passed": False,
            "errors": [f"policy contract 无法加载: {type(exc).__name__}: {exc}"],
            "warnings": [],
        }

    _append_mismatch(
        errors,
        "contract schema_version",
        contract.get("schema_version"),
        1,
    )
    reference = contract.get("reference_contract", {})
    _append_mismatch(
        errors,
        "sample_id",
        manifest.get("id"),
        reference.get("sample_id"),
    )
    if not manifest_path.is_file():
        errors.append(f"manifest 不存在: {manifest_path}")
    else:
        _append_mismatch(
            errors,
            "manifest sha256",
            file_fingerprint(manifest_path)["sha256"],
            reference.get("manifest_sha256"),
        )

    actual_presets = preset_models or preset_model_policy()
    _append_mismatch(
        errors,
        "production presets",
        actual_presets,
        contract.get("production_presets"),
    )
    actual_policy_models = sorted({
        policy.get("model")
        for policy in manifest.get("policies", [])
        if policy.get("model")
    })
    _append_mismatch(
        errors,
        "benchmark policy models",
        actual_policy_models,
        sorted(contract.get("required_policy_models", [])),
    )

    reference_root = manifest_path.parent
    for relative, expected_hash in reference.get("files", {}).items():
        path = reference_root / relative
        if not path.is_file():
            errors.append(f"固定参考文件不存在: {path}")
            continue
        _append_mismatch(
            errors,
            f"{relative} sha256",
            file_fingerprint(path)["sha256"],
            expected_hash,
        )

    segments_path = reference_root / manifest["reference"]["segments_json"]
    if segments_path.is_file():
        payload = _read_json(segments_path)
        segments = payload.get("segments", [])
        word_count = sum(
            len(segment.get("words", [])) for segment in segments)
        speaker_count = len({
            segment.get("speaker")
            for segment in segments
            if segment.get("speaker")
        })
        _append_mismatch(
            errors,
            "reference segment_count",
            len(segments),
            reference.get("segment_count"),
        )
        _append_mismatch(
            errors,
            "reference word_count",
            word_count,
            reference.get("word_count"),
        )
        _append_mismatch(
            errors,
            "reference speaker_count",
            speaker_count,
            reference.get("speaker_count"),
        )
        _append_mismatch(
            errors,
            "reference duration_seconds",
            payload.get("clip", {}).get("duration_seconds"),
            reference.get("duration_seconds"),
        )

    audio_contract = reference.get("audio", {})
    audio_path = reference_root / audio_contract.get("path", "")
    if audio_path.is_file():
        fingerprint = file_fingerprint(audio_path)
        _append_mismatch(
            errors,
            "prepared audio bytes",
            fingerprint["bytes"],
            audio_contract.get("bytes"),
        )
        _append_mismatch(
            errors,
            "prepared audio sha256",
            fingerprint["sha256"],
            audio_contract.get("sha256"),
        )
    else:
        warnings.append(
            f"prepared audio 未安装，跳过二进制校验: {audio_path}")

    source_presence = [
        (reference_root / relative).is_file()
        for relative in EXPECTED_HASHES
    ]
    if any(source_presence) and not all(source_presence):
        errors.append("AMI 上游 source artifacts 不完整")
    elif all(source_presence):
        try:
            verify_sources(reference_root)
        except (OSError, RuntimeError) as exc:
            errors.append(f"AMI 上游 source 校验失败: {exc}")
    else:
        warnings.append("AMI 上游 source artifacts 未安装，跳过 source 校验")

    selection = manifest.get("policy_selection", {})
    comparisons = contract.get("acceptance", {}).get("comparisons", [])
    if comparisons:
        comparison = comparisons[0]
        _append_mismatch(
            errors,
            "max_cpwer_regression",
            selection.get("max_cpwer_regression"),
            comparison.get("max_cpwer_regression"),
        )
        _append_mismatch(
            errors,
            "max_speaker_attributed_wer_regression",
            selection.get("max_speaker_attributed_wer_regression"),
            comparison.get("max_speaker_attributed_wer_regression"),
        )
        _append_mismatch(
            errors,
            "min_speedup",
            selection.get("min_speedup"),
            comparison.get("min_speedup"),
        )

    return {
        "schema_version": 1,
        "passed": not errors,
        "contract": str(contract_path),
        "manifest": str(manifest_path),
        "errors": errors,
        "warnings": warnings,
    }


def verify_report_payload(contract, report):
    errors = []
    reference = contract.get("reference_contract", {})
    _append_mismatch(
        errors,
        "report schema_version",
        report.get("schema_version"),
        2,
    )
    _append_mismatch(
        errors,
        "report sample_id",
        report.get("sample_id"),
        reference.get("sample_id"),
    )
    inputs = report.get("inputs", {})
    _append_mismatch(
        errors,
        "report manifest sha256",
        inputs.get("manifest", {}).get("sha256"),
        reference.get("manifest_sha256"),
    )
    audio_contract = reference.get("audio", {})
    _append_mismatch(
        errors,
        "report audio sha256",
        inputs.get("audio", {}).get("sha256"),
        audio_contract.get("sha256"),
    )
    _append_mismatch(
        errors,
        "report audio bytes",
        inputs.get("audio", {}).get("bytes"),
        audio_contract.get("bytes"),
    )
    expected_reference_hashes = sorted(
        reference.get("files", {}).values())
    actual_reference_hashes = sorted(
        fingerprint.get("sha256")
        for fingerprint in inputs.get("references", {}).values()
        if fingerprint.get("sha256")
    )
    _append_mismatch(
        errors,
        "report reference hashes",
        actual_reference_hashes,
        expected_reference_hashes,
    )

    runs = {
        run.get("model"): run
        for run in report.get("runs", [])
        if run.get("model")
    }
    for model in contract.get("required_policy_models", []):
        run = runs.get(model)
        if not run:
            errors.append(f"report 缺少 policy model: {model}")
        elif run.get("status") != "passed":
            errors.append(
                f"policy model 未通过: {model}: {run.get('error')}")

    recommendation = report.get("recommendation", {})
    for key, expected in contract.get(
            "expected_recommendation", {}).items():
        _append_mismatch(
            errors,
            f"recommendation {key}",
            recommendation.get(key),
            expected,
        )

    acceptance = contract.get("acceptance", {})
    shared = report.get("shared_diarization", {})
    for key, expected in acceptance.get(
            "shared_diarization", {}).items():
        _append_mismatch(
            errors,
            f"shared diarization {key}",
            shared.get(key),
            expected,
        )

    successful_runs = [
        run for run in runs.values()
        if run.get("status") == "passed"
    ]
    diarization = (
        successful_runs[0].get("metrics", {}).get("diarization", {})
        if successful_runs else {}
    )
    strict = acceptance.get("strict_diarization", {})
    _check_upper(
        errors,
        "strict DER",
        diarization.get("der"),
        strict.get("max_der"),
    )
    _check_upper(
        errors,
        "strict JER",
        diarization.get("jer"),
        strict.get("max_jer"),
    )

    for model, limits in acceptance.get("models", {}).items():
        run = runs.get(model, {})
        metrics = run.get("metrics", {})
        meta = run.get("asr_meta", {})
        _check_upper(
            errors,
            f"{model} cpWER",
            metrics.get("cpwer", {}).get("wer"),
            limits["max_cpwer"],
        )
        _check_upper(
            errors,
            f"{model} speaker-attributed WER",
            metrics.get("speaker_attributed_wer", {}).get("wer"),
            limits["max_speaker_attributed_wer"],
        )
        _check_lower(
            errors,
            f"{model} number recall",
            metrics.get("lexical", {}).get("number_recall"),
            limits["min_number_recall"],
        )
        _check_lower(
            errors,
            f"{model} entity recall",
            metrics.get("lexical", {}).get("entity_recall"),
            limits["min_entity_recall"],
        )
        _check_lower(
            errors,
            f"{model} alignment coverage",
            meta.get("alignment", {}).get("word_timestamp_coverage"),
            limits["min_alignment_coverage"],
        )
        _check_upper(
            errors,
            f"{model} start timestamp MAE",
            metrics.get("timestamps", {}).get("start_mae_seconds"),
            limits["max_start_mae_seconds"],
        )
        _check_upper(
            errors,
            f"{model} end timestamp MAE",
            metrics.get("timestamps", {}).get("end_mae_seconds"),
            limits["max_end_mae_seconds"],
        )

    for comparison in acceptance.get("comparisons", []):
        faster_model = comparison["faster_model"]
        reference_model = comparison["reference_model"]
        faster = runs.get(faster_model, {})
        reference_run = runs.get(reference_model, {})
        faster_wall = faster.get("wall_seconds")
        reference_wall = reference_run.get("wall_seconds")
        if (
                isinstance(faster_wall, (int, float))
                and isinstance(reference_wall, (int, float))
                and faster_wall > 0):
            speedup = reference_wall / faster_wall
        else:
            speedup = None
        _check_lower(
            errors,
            f"{faster_model} speedup vs {reference_model}",
            speedup,
            comparison["min_speedup"],
        )
        faster_metrics = faster.get("metrics", {})
        reference_metrics = reference_run.get("metrics", {})
        cpwer_regression = None
        sawer_regression = None
        faster_cpwer = faster_metrics.get("cpwer", {}).get("wer")
        reference_cpwer = reference_metrics.get("cpwer", {}).get("wer")
        faster_sawer = faster_metrics.get(
            "speaker_attributed_wer", {}).get("wer")
        reference_sawer = reference_metrics.get(
            "speaker_attributed_wer", {}).get("wer")
        if all(isinstance(value, (int, float)) for value in (
                faster_cpwer, reference_cpwer)):
            cpwer_regression = faster_cpwer - reference_cpwer
        if all(isinstance(value, (int, float)) for value in (
                faster_sawer, reference_sawer)):
            sawer_regression = faster_sawer - reference_sawer
        _check_upper(
            errors,
            f"{faster_model} cpWER regression",
            cpwer_regression,
            comparison["max_cpwer_regression"],
        )
        _check_upper(
            errors,
            f"{faster_model} speaker-attributed WER regression",
            sawer_regression,
            comparison["max_speaker_attributed_wer_regression"],
        )

    return errors


def verify_benchmark_report(
    contract_path=DEFAULT_CONTRACT,
    report_path=None,
    preset_models=None,
):
    check = check_policy_contract(
        contract_path,
        preset_models=preset_models,
    )
    if not check["passed"]:
        return check
    (
        contract_path,
        contract,
        manifest_path,
        manifest,
    ) = _contract_context(contract_path)
    report_path = Path(
        report_path or _default_report_path(manifest_path, manifest))
    if not report_path.is_file():
        return {
            **check,
            "passed": False,
            "report": str(report_path),
            "errors": [f"benchmark report 不存在: {report_path}"],
        }
    report = _read_json(report_path)
    errors = verify_report_payload(contract, report)
    return {
        **check,
        "passed": not errors,
        "report": str(report_path.resolve()),
        "errors": errors,
        "recommendation": report.get("recommendation"),
    }


def run_benchmark_workflow(
    contract_path=DEFAULT_CONTRACT,
    *,
    reuse_shared_diarization=False,
):
    (
        contract_path,
        contract,
        manifest_path,
        manifest,
    ) = _contract_context(contract_path)
    reference = contract["reference_contract"]
    prepare(
        manifest_path.parent,
        start=float(reference["start_seconds"]),
        duration=float(reference["duration_seconds"]),
    )
    preflight = check_policy_contract(contract_path)
    if not preflight["passed"]:
        return {
            **preflight,
            "stage": "contract_check",
        }
    report = run_manifest(
        manifest_path,
        reuse_shared_diarization=reuse_shared_diarization,
    )
    errors = verify_report_payload(contract, report)
    result = {
        "schema_version": 1,
        "passed": not errors,
        "contract": str(contract_path),
        "manifest": str(manifest_path),
        "report": str(_default_report_path(manifest_path, manifest)),
        "errors": errors,
        "recommendation": report.get("recommendation"),
    }
    verification_path = (
        _default_report_path(manifest_path, manifest).parent
        / "policy_verification.json"
    )
    atomic_write_json(verification_path, result)
    result["verification"] = str(verification_path)
    return result


def rescore_benchmark_workflow(contract_path=DEFAULT_CONTRACT):
    (
        contract_path,
        contract,
        manifest_path,
        manifest,
    ) = _contract_context(contract_path)
    preflight = check_policy_contract(contract_path)
    if not preflight["passed"]:
        return {
            **preflight,
            "stage": "contract_check",
        }
    report = rescore_manifest(manifest_path)
    errors = verify_report_payload(contract, report)
    result = {
        "schema_version": 1,
        "passed": not errors,
        "contract": str(contract_path),
        "manifest": str(manifest_path),
        "report": str(_default_report_path(manifest_path, manifest)),
        "errors": errors,
        "recommendation": report.get("recommendation"),
    }
    verification_path = (
        _default_report_path(manifest_path, manifest).parent
        / "policy_verification.json"
    )
    atomic_write_json(verification_path, result)
    result["verification"] = str(verification_path)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="执行和校验 contract-driven ASR benchmark workflow")
    parser.add_argument(
        "--contract",
        default=str(DEFAULT_CONTRACT),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "check",
        help="校验固定参考、manifest 和生产 preset，不运行模型",
    )
    verify = subparsers.add_parser(
        "verify",
        help="校验已有 benchmark report 是否满足 contract",
    )
    verify.add_argument("--report", default=None)
    subparsers.add_parser(
        "rescore",
        help="复用已有 hypotheses 重算指标并执行验收",
    )
    run = subparsers.add_parser(
        "run",
        help="准备样本、运行所有 policy 并执行验收",
    )
    run.add_argument(
        "--reuse-shared-diarization",
        action="store_true",
    )
    args = parser.parse_args()
    if args.command == "check":
        result = check_policy_contract(args.contract)
    elif args.command == "verify":
        result = verify_benchmark_report(
            args.contract,
            report_path=args.report,
        )
    elif args.command == "run":
        result = run_benchmark_workflow(
            args.contract,
            reuse_shared_diarization=args.reuse_shared_diarization,
        )
    else:
        result = rescore_benchmark_workflow(args.contract)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
