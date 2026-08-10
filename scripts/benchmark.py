"""可重复的 ASR/总结质量基准工具。

ASR 基准需要人工参考文本；本仓库不伪造参考答案。总结基准复用
content_map.json/summary_map.json 的内容覆盖率。
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

try:
    from rapidfuzz.distance import Levenshtein
except ImportError:
    Levenshtein = None

try:
    from content_map import coverage_report, load_json
except ImportError:  # package import
    from scripts.content_map import coverage_report, load_json

try:
    from asr_benchmark import benchmark_sample
except ImportError:
    from scripts.asr_benchmark import benchmark_sample


def _words(text):
    return re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", text.lower())


def _numbers(text):
    return re.findall(r"(?<![a-z])\$?\d+(?:[,.]\d+)*(?:%|x|k|m|b)?", text.lower())


def _edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, 1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ref_word != hyp_word),
            ))
        previous = current
    return previous[-1]


def asr_metrics(reference_text, hypothesis_text):
    reference = _words(reference_text)
    hypothesis = _words(hypothesis_text)
    distance = (
        Levenshtein.distance(reference, hypothesis)
        if Levenshtein is not None
        else _edit_distance(reference, hypothesis)
    )
    ref_numbers = Counter(_numbers(reference_text))
    hyp_numbers = Counter(_numbers(hypothesis_text))
    matched_numbers = sum((ref_numbers & hyp_numbers).values())
    return {
        "reference_words": len(reference),
        "hypothesis_words": len(hypothesis),
        "edit_distance": distance,
        "wer": round(distance / len(reference), 4) if reference else None,
        "reference_numbers": sorted(ref_numbers.elements()),
        "hypothesis_numbers": sorted(hyp_numbers.elements()),
        "number_recall": round(matched_numbers / sum(ref_numbers.values()), 4)
        if ref_numbers else 1.0,
    }


def summary_metrics(content_map, summary_map):
    coverage = coverage_report(content_map, summary_map)
    return {
        "high_coverage": coverage["high_coverage"],
        "medium_coverage": coverage["medium_coverage"],
        "high_missing": coverage["high_missing"],
        "unsupported_units": coverage["unsupported_units"],
        "passed": coverage["passed"],
    }


def main():
    parser = argparse.ArgumentParser(description="ASR/总结质量基准")
    sub = parser.add_subparsers(dest="command", required=True)
    asr = sub.add_parser("asr", help="比较参考英文稿和 ASR 输出")
    asr.add_argument("reference")
    asr.add_argument("hypothesis")
    summary = sub.add_parser("summary", help="检查内容单元覆盖率")
    summary.add_argument("content_map")
    summary.add_argument("summary_map")
    suite = sub.add_parser(
        "suite", help="运行多说话人 ASR benchmark manifest")
    suite.add_argument("manifest")
    suite.add_argument("hypothesis")
    args = parser.parse_args()

    if args.command == "asr":
        result = asr_metrics(
            Path(args.reference).read_text(encoding="utf-8"),
            Path(args.hypothesis).read_text(encoding="utf-8"),
        )
    elif args.command == "summary":
        result = summary_metrics(load_json(args.content_map), load_json(args.summary_map))
    else:
        manifest_path = Path(args.manifest)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for section in ("reference",):
            for key in ("segments_json", "stm", "rttm"):
                value = manifest.get(section, {}).get(key)
                if value and not Path(value).is_absolute():
                    manifest[section][key] = str(
                        (manifest_path.parent / value).resolve())
        hypothesis = json.loads(
            Path(args.hypothesis).read_text(encoding="utf-8"))
        result = benchmark_sample(manifest, hypothesis)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
