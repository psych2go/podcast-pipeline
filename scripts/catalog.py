#!/usr/bin/env python3
"""Podcast catalog CLI and compatibility exports.

All implementation lives in catalog_core, catalog_site, catalog_health, and
catalog_publish. Callers that need alternate paths should configure a
CatalogPaths instance explicitly instead of monkeypatching this facade.
"""
import argparse

try:
    from atomic_io import atomic_write_text
    from catalog_core import (
        CATALOG, CONTENT_DIR, SITE_DIR, CatalogPaths, _audio_duration_minutes,
        _catalog_text, _display_title, _episode_dirs, _find_briefing,
        _gen_mp3, _load_site_entries, _ordered_episode_names, _read_source,
        _source_cell, _zh_chars, add_to_catalog,
        episode_stats, rebuild_catalog,
    )
    from catalog_health import build_health_report as _build_health_report
    from catalog_publish import (
        BASE_DIR, PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        _PublishFailure, _batch_publish_item, _candidate_catalog_errors,
        _dotenv_assignments, _finish_batch_impl, _finish_impl,
        _is_wrangler_command, _publish_preflight, _release_report, _run,
        _run_with_output, _run_wrangler, _upload_r2_item,
        _verify_publish_with_retry, _wrangler_environment,
        _write_publish_failure, configure_paths, finish, finish_batch,
    )
    from catalog_site import (
        _build_entry, _site_readiness_errors, backfill_sources,
        catalog_consistency_errors, gen_index, sync_site,
    )
    from config import (
        BASE_DIR as CONFIG_CONTENT_DIR,
        CATALOG_PATH as CONFIG_CATALOG,
        PROJECT_ROOT as CONFIG_ROOT,
        SITE_DIR as CONFIG_SITE_DIR,
    )
except ImportError:
    from scripts.atomic_io import atomic_write_text
    from scripts.catalog_core import (
        CATALOG, CONTENT_DIR, SITE_DIR, CatalogPaths, _audio_duration_minutes,
        _catalog_text, _display_title, _episode_dirs, _find_briefing,
        _gen_mp3, _load_site_entries, _ordered_episode_names, _read_source,
        _source_cell, _zh_chars, add_to_catalog,
        episode_stats, rebuild_catalog,
    )
    from scripts.catalog_health import build_health_report as _build_health_report
    from scripts.catalog_publish import (
        BASE_DIR, PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        _PublishFailure, _batch_publish_item, _candidate_catalog_errors,
        _dotenv_assignments, _finish_batch_impl, _finish_impl,
        _is_wrangler_command, _publish_preflight, _release_report, _run,
        _run_with_output, _run_wrangler, _upload_r2_item,
        _verify_publish_with_retry, _wrangler_environment,
        _write_publish_failure, configure_paths, finish, finish_batch,
    )
    from scripts.catalog_site import (
        _build_entry, _site_readiness_errors, backfill_sources,
        catalog_consistency_errors, gen_index, sync_site,
    )
    from scripts.config import (
        BASE_DIR as CONFIG_CONTENT_DIR,
        CATALOG_PATH as CONFIG_CATALOG,
        PROJECT_ROOT as CONFIG_ROOT,
        SITE_DIR as CONFIG_SITE_DIR,
    )


def _configure_cli_paths():
    """Bind the facade and deep catalog modules to one private workspace."""
    global BASE_DIR, CONTENT_DIR, SITE_DIR, CATALOG
    paths = CatalogPaths(
        base_dir=CONFIG_ROOT,
        content_dir=CONFIG_CONTENT_DIR,
        site_dir=CONFIG_SITE_DIR,
        catalog=CONFIG_CATALOG,
    )
    configure_paths(paths)
    BASE_DIR = paths.base_dir
    CONTENT_DIR = paths.content_dir
    SITE_DIR = paths.site_dir
    CATALOG = paths.catalog
    return paths


def build_health_report(content_dir=None, since="7d", now=None):
    return _build_health_report(content_dir or CONTENT_DIR, since=since, now=now)


def health(since="7d", output=None):
    text = build_health_report(since=since)
    if output:
        atomic_write_text(output, text)
        print(f"[健康报告] 已写入 {output}")
    print(text, end="")
    return text


def main():
    _configure_cli_paths()
    parser = argparse.ArgumentParser(description="播客台账与完整发布维护")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stats", help="打印某期字数/时长，不写入"); p.add_argument("name")
    p = sub.add_parser("add", help="追加/更新某期到播客目录"); p.add_argument("name")
    sub.add_parser("rebuild", help="从当前内容和 site 顺序全量重建播客目录")
    sub.add_parser("check", help="校验播客目录、site.json 与当前内容统计一致")
    p = sub.add_parser("sync-site", help="同步 content.html + 重建 site.json"); p.add_argument("--only", default=None)
    sub.add_parser("gen-index", help="从 site.json 重建首页 index.html")
    sub.add_parser("backfill-sources", help="为缺来源信息的期回填来源")
    p = sub.add_parser("health", help="汇总近期跨单集运行健康度"); p.add_argument("--since", default="7d"); p.add_argument("--output", default=None)
    p = sub.add_parser("finish", help="完整发布：R2 + Pages + 远端验收"); p.add_argument("name"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("finish-batch", help="批量完整发布：R2 + Pages + 逐期远端验收"); p.add_argument("names", nargs="+"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--upload-concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.cmd == "stats":
        stats = episode_stats(args.name); print(f"{args.name}: {stats['chars']//1000}K字, {stats['duration']}min")
    elif args.cmd == "add": add_to_catalog(args.name)
    elif args.cmd == "rebuild": rebuild_catalog()
    elif args.cmd == "check":
        errors = catalog_consistency_errors()
        for error in errors: print(f"[一致性][错误] {error}")
        if not errors: print("[一致性] 播客目录、site.json 与当前内容统计一致")
        return 1 if errors else 0
    elif args.cmd == "sync-site": sync_site(args.only)
    elif args.cmd == "gen-index": gen_index()
    elif args.cmd == "backfill-sources": backfill_sources()
    elif args.cmd == "health":
        try: health(args.since, args.output)
        except ValueError as exc: parser.error(str(exc))
    elif args.cmd == "finish": return 0 if finish(args.name, args.dry_run) else 1
    elif args.cmd == "finish-batch": return 0 if finish_batch(args.names, args.dry_run, upload_concurrency=args.upload_concurrency) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
