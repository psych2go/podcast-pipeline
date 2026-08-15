"""Read-only aggregation of per-episode run and quality reports."""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from run_report import RUN_REPORT_SCHEMA_VERSION
    from sources import source_host
except ImportError:
    from scripts.run_report import RUN_REPORT_SCHEMA_VERSION
    from scripts.sources import source_host


def _load_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_since(value):
    match = re.fullmatch(r"(\d+)([dhw])", value.strip().lower())
    if not match:
        raise ValueError("--since 必须使用 24h、7d 或 2w 形式")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since 必须大于 0")
    return {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }[match.group(2)]


def _metric_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(
        value, bool) else 0


def _error_key(value):
    return " ".join(str(value or "").split())[:180]


def _md_cell(value):
    return str(value).replace("|", r"\|").replace("\n", " ")


def build_health_report(content_dir, since="7d", now=None):
    """Aggregate valid strict-mode run reports without mutating state."""
    content_dir = Path(content_dir)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - _parse_since(since)
    stage_stats = defaultdict(
        lambda: {"runs": 0, "failures": 0, "durations": []})
    error_counts = Counter()
    source_failures = Counter()
    total_runs = 0
    retry_count = 0
    tls_downgrades = 0
    reported_cost = 0.0
    token_usage = Counter()
    unpublished = []

    for report_path in sorted(content_dir.glob("*/run_report.json")):
        folder = report_path.parent
        episode = _load_json_file(folder / "episode.json")
        if episode.get("quality", {}).get("mode") == "legacy":
            continue
        payload = _load_json_file(report_path)
        if payload.get("schema_version") != RUN_REPORT_SCHEMA_VERSION:
            continue
        eligible_runs = []
        for run in payload.get("runs", []):
            metadata = run.get("metadata", {})
            if (
                    run.get("status") not in {"passed", "failed"}
                    or metadata.get("skip_health")
                    or metadata.get("legacy")):
                continue
            started_at = _parse_timestamp(
                run.get("started_at") or run.get("completed_at"))
            if started_at is None or started_at < cutoff:
                continue
            eligible_runs.append(run)
            total_runs += 1
            source = metadata.get("source", "")
            if run.get("status") == "failed" and source:
                source_failures[source_host(source) or "unknown"] += 1

            stages = run.get("stages") or [{
                "name": run.get("command", "unknown"),
                "status": run.get("status"),
                "duration_seconds": run.get("duration_seconds"),
                "error": run.get("error"),
                "metrics": run.get("metrics", {}),
            }]
            failed_stage_seen = False
            for stage in stages:
                name = stage.get("name") or "unknown"
                stats = stage_stats[name]
                stats["runs"] += 1
                if stage.get("status") == "failed":
                    stats["failures"] += 1
                    failed_stage_seen = True
                    error = _error_key(stage.get("error"))
                    if error:
                        error_counts[error] += 1
                duration = stage.get("duration_seconds")
                if isinstance(duration, (int, float)):
                    stats["durations"].append(duration)
                metrics = stage.get("metrics", {})
                retry_count += int(_metric_number(metrics.get("retry_count")))
                tls_downgrades += int(bool(metrics.get("tls_downgrade")))
                reported_cost += _metric_number(metrics.get("reported_cost_usd"))
                usage = metrics.get("usage", {})
                if isinstance(usage, dict):
                    for key in (
                            "input_tokens", "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens"):
                        token_usage[key] += int(_metric_number(usage.get(key)))
            if run.get("status") == "failed" and not failed_stage_seen:
                error = _error_key(run.get("error"))
                if error:
                    error_counts[error] += 1

        if not eligible_runs:
            continue
        quality = _load_json_file(folder / "quality_report.json")
        published = _load_json_file(folder / "publish_report.json")
        if quality and not quality.get("passed") and not published.get("passed"):
            failed_runs = [
                run for run in eligible_runs if run.get("status") == "failed"]
            latest_failure = max(
                failed_runs or eligible_runs,
                key=lambda run: _parse_timestamp(
                    run.get("started_at") or run.get("completed_at")) or cutoff,
            )
            failed_stages = [
                stage for stage in latest_failure.get("stages", [])
                if stage.get("status") == "failed"
            ]
            last_stage = (
                failed_stages[-1].get("name") if failed_stages
                else latest_failure.get("command", "unknown")
            )
            reason = (
                failed_stages[-1].get("error") if failed_stages
                else latest_failure.get("error")
            ) or next(iter(quality.get("errors", [])), "质量门未通过")
            unpublished.append((folder.name, last_stage, _error_key(reason)))

    lines = [
        "# Pipeline Health", "",
        f"- 时间窗口：{cutoff.isoformat()} 至 {now.isoformat()}",
        f"- 有效运行：{total_runs}",
        f"- 重试总数：{retry_count}",
        f"- TLS 降级次数：{tls_downgrades}",
        f"- AI 已报告成本：${reported_cost:.4f}",
        (
            "- Token：input "
            f"{token_usage['input_tokens']} / output "
            f"{token_usage['output_tokens']} / cache read "
            f"{token_usage['cache_read_input_tokens']}"
        ),
        "", "## 阶段健康度", "",
        "| 阶段 | 运行数 | 失败数 | 失败率 | 平均耗时 |",
        "|---|---:|---:|---:|---:|",
    ]
    if stage_stats:
        for name, stats in sorted(stage_stats.items()):
            average = (
                sum(stats["durations"]) / len(stats["durations"])
                if stats["durations"] else 0
            )
            failure_rate = stats["failures"] / stats["runs"] * 100
            lines.append(
                f"| {_md_cell(name)} | {stats['runs']} | "
                f"{stats['failures']} | {failure_rate:.1f}% | "
                f"{average:.1f}s |"
            )
    else:
        lines.append("| 无数据 | 0 | 0 | 0.0% | 0.0s |")

    lines.extend(["", "## 高频错误", "", "| 次数 | 错误 |", "|---:|---|"])
    if error_counts:
        for error, count in error_counts.most_common(10):
            lines.append(f"| {count} | {_md_cell(error)} |")
    else:
        lines.append("| 0 | 无 |")

    lines.extend(["", "## 失败来源", "", "| 次数 | 域名 |", "|---:|---|"])
    if source_failures:
        for host, count in source_failures.most_common(10):
            lines.append(f"| {count} | {_md_cell(host)} |")
    else:
        lines.append("| 0 | 无 |")

    lines.extend([
        "", "## 未发布且质量门未通过", "",
        "| 单集 | 最后阶段 | 原因 |", "|---|---|---|",
    ])
    if unpublished:
        for name, stage, reason in sorted(unpublished):
            lines.append(
                f"| {_md_cell(name)} | {_md_cell(stage)} | {_md_cell(reason)} |")
    else:
        lines.append("| 无 | - | - |")
    return "\n".join(lines) + "\n"
