#!/usr/bin/env python3
"""Podcast catalog CLI and backward-compatible facade.

Implementation lives in catalog_core, catalog_site, and catalog_publish.  This
module deliberately keeps the historical names patchable for callers and tests.
"""
import argparse
import sys
import threading
from pathlib import Path

try:
    import catalog_core as _core
    import catalog_site as _site
    import catalog_publish as _publish
    from atomic_io import atomic_write_text
    from catalog_health import build_health_report as _build_health_report
    from config import (
        PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        validate_for_stage,
    )
    from episode import (
        audio_key as episode_audio_key,
        display_title as episode_display_title,
        legacy_page_path as episode_legacy_page_path,
        page_path as episode_page_path,
        public_audio_url,
        quality_metadata,
        source_metadata,
    )
    from publish import (
        PUBLISH_REPORT_SCHEMA_VERSION, verify_publish, write_publish_report,
    )
    from release import active_audio_key, load_release, update_release_state
    from run_report import RunReport
except ImportError:
    from scripts import catalog_core as _core
    from scripts import catalog_site as _site
    from scripts import catalog_publish as _publish
    from scripts.atomic_io import atomic_write_text
    from scripts.catalog_health import build_health_report as _build_health_report
    from scripts.config import (
        PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        validate_for_stage,
    )
    from scripts.episode import (
        audio_key as episode_audio_key,
        display_title as episode_display_title,
        legacy_page_path as episode_legacy_page_path,
        page_path as episode_page_path,
        public_audio_url,
        quality_metadata,
        source_metadata,
    )
    from scripts.publish import (
        PUBLISH_REPORT_SCHEMA_VERSION, verify_publish, write_publish_report,
    )
    from scripts.release import active_audio_key, load_release, update_release_state
    from scripts.run_report import RunReport

# Compatibility exports used by existing integrations and tests.
subprocess = _publish.subprocess
time = _publish.time
BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
SITE_DIR = BASE_DIR / "site"
CATALOG = CONTENT_DIR / "播客目录.md"
MAX_DURATION_MB_PER_MIN = _core.MAX_DURATION_MB_PER_MIN
CATALOG_HEADER = _core.CATALOG_HEADER
_PublishFailure = _publish._PublishFailure

_CORE_IMPLS = {
    name: getattr(_core, name)
    for name in (
        "_zh_chars", "_find_briefing", "_gen_mp3", "_audio_duration_minutes",
        "episode_stats", "_read_source", "_source_cell", "_display_title",
        "_load_site_entries", "_ordered_episode_names", "_catalog_text",
        "rebuild_catalog",
    )
}
_SITE_IMPLS = {
    name: getattr(_site, name)
    for name in (
        "_episode_dirs", "_build_entry", "_site_readiness_errors", "sync_site",
        "catalog_consistency_errors", "gen_index", "backfill_sources",
    )
}
_PUBLISH_IMPLS = {
    name: getattr(_publish, name)
    for name in (
        "_is_wrangler_command", "_dotenv_assignments", "_wrangler_environment",
        "_run_with_output", "_run", "_run_wrangler",
        "_verify_publish_with_retry", "_release_report",
        "_write_publish_failure", "_candidate_catalog_errors",
        "_publish_preflight", "_finish_impl", "_batch_publish_item",
        "_upload_r2_item", "_finish_batch_impl",
    )
}
_FACADE_DEFAULTS = {}
_RUNTIME_LOCK = threading.RLock()


def _current_paths():
    return _core.CatalogPaths(
        base_dir=BASE_DIR, content_dir=CONTENT_DIR,
        site_dir=SITE_DIR, catalog=CATALOG)


def _configured(name, implementation):
    value = globals()[name]
    return implementation if value is _FACADE_DEFAULTS.get(name) else value


def _sync_core():
    _core.configure_paths(_current_paths())
    _core.MAX_DURATION_MB_PER_MIN = MAX_DURATION_MB_PER_MIN
    _core.episode_stats = (
        globals()["episode_stats"]
        if globals()["episode_stats"] is not _FACADE_DEFAULTS.get(
            "episode_stats")
        else _CORE_IMPLS["episode_stats"]
    )


def _sync_site():
    _sync_core()
    _site.configure_paths(_current_paths())
    for name in (
        "_find_briefing", "_gen_mp3", "_read_source", "_display_title",
        "episode_stats", "_load_site_entries", "_ordered_episode_names",
        "_catalog_text", "_episode_dirs", "_build_entry",
        "_site_readiness_errors",
    ):
        setattr(_site, name, globals()[name])


def _sync_publish():
    _publish.configure_paths(_current_paths())
    for name in (
        "PAGES_BASE_URL", "PAGES_PROJECT", "R2_BUCKET", "R2_PUBLIC_URL",
        "validate_for_stage", "episode_audio_key", "episode_page_path",
        "public_audio_url", "verify_publish", "write_publish_report",
        "active_audio_key", "load_release", "update_release_state",
    ):
        setattr(_publish, name, globals()[name])
    for name in (
        "_find_briefing", "_gen_mp3", "_display_title", "_load_site_entries",
        "_ordered_episode_names", "_site_readiness_errors", "_build_entry",
        "_catalog_text", "sync_site", "rebuild_catalog", "gen_index",
        "catalog_consistency_errors", "_run_with_output", "_run",
        "_run_wrangler", "_verify_publish_with_retry", "_release_report",
        "_write_publish_failure", "_candidate_catalog_errors",
        "_publish_preflight", "_batch_publish_item", "_upload_r2_item",
    ):
        if name in _PUBLISH_IMPLS:
            setattr(_publish, name, _configured(name, _PUBLISH_IMPLS[name]))
        else:
            # Cross-module collaborators go through the facade so patched paths
            # and compatibility mocks remain effective.
            setattr(_publish, name, globals()[name])


def _core_call(name, *args, **kwargs):
    with _RUNTIME_LOCK:
        _sync_core()
        return _CORE_IMPLS[name](*args, **kwargs)


def _site_call(name, *args, **kwargs):
    with _RUNTIME_LOCK:
        _sync_site()
        return _SITE_IMPLS[name](*args, **kwargs)


def _zh_chars(value): return _core_call("_zh_chars", value)
def _find_briefing(folder): return _core_call("_find_briefing", folder)
def _gen_mp3(folder): return _core_call("_gen_mp3", folder)
def _audio_duration_minutes(mp3): return _core_call("_audio_duration_minutes", mp3)
def episode_stats(name): return _core_call("episode_stats", name)
def _read_source(name): return _core_call("_read_source", name)
def _source_cell(name): return _core_call("_source_cell", name)
def _display_title(name): return _core_call("_display_title", name)
def _load_site_entries(): return _core_call("_load_site_entries")
def _episode_dirs(): return _site_call("_episode_dirs")
def _ordered_episode_names(): return _core_call("_ordered_episode_names")
def _catalog_text(names): return _core_call("_catalog_text", names)
def rebuild_catalog(): return _core_call("rebuild_catalog")


def add_to_catalog(name):
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        sys.exit(f"[错误] 找不到播客目录: {folder}")
    if not _find_briefing(folder):
        sys.exit(f"[错误] 找不到讲稿: {folder}")
    return rebuild_catalog()


def _build_entry(name, previous):
    return _site_call("_build_entry", name, previous)


def _site_readiness_errors(names, existing):
    return _site_call("_site_readiness_errors", names, existing)


def sync_site(only=None):
    return _site_call("sync_site", only)


def catalog_consistency_errors():
    return _site_call("catalog_consistency_errors")


def gen_index():
    return _site_call("gen_index")


def backfill_sources():
    return _site_call("backfill_sources")


def build_health_report(content_dir=None, since="7d", now=None):
    return _build_health_report(content_dir or CONTENT_DIR, since=since, now=now)


def health(since="7d", output=None):
    text = build_health_report(since=since)
    if output:
        atomic_write_text(output, text)
        print(f"[健康报告] 已写入 {output}")
    print(text, end="")
    return text


def _publish_call(name, *args, **kwargs):
    with _RUNTIME_LOCK:
        _sync_publish()
        return _PUBLISH_IMPLS[name](*args, **kwargs)


def _is_wrangler_command(cmd): return _publish_call("_is_wrangler_command", cmd)
def _dotenv_assignments(path): return _publish_call("_dotenv_assignments", path)
def _wrangler_environment(): return _publish_call("_wrangler_environment")
def _run_with_output(cmd, cwd, dry_run=False): return _publish_call("_run_with_output", cmd, cwd, dry_run)
def _run(cmd, cwd, dry_run=False): return _publish_call("_run", cmd, cwd, dry_run)
def _run_wrangler(cmd, dry_run=False): return _publish_call("_run_wrangler", cmd, dry_run)
def _verify_publish_with_retry(*args, attempts=4, delay=3): return _publish_call("_verify_publish_with_retry", *args, attempts=attempts, delay=delay)
def _release_report(folder, audio_key=None): return _publish_call("_release_report", folder, audio_key)
def _write_publish_failure(*args, **kwargs): return _publish_call("_write_publish_failure", *args, **kwargs)
def _candidate_catalog_errors(name): return _publish_call("_candidate_catalog_errors", name)
def _publish_preflight(name): return _publish_call("_publish_preflight", name)
def _finish_impl(name, dry_run, run_report): return _publish_call("_finish_impl", name, dry_run, run_report)
def _batch_publish_item(name): return _publish_call("_batch_publish_item", name)
def _upload_r2_item(item, dry_run=False): return _publish_call("_upload_r2_item", item, dry_run)
def _finish_batch_impl(names, dry_run=False, *, upload_concurrency=3): return _publish_call("_finish_batch_impl", names, dry_run, upload_concurrency=upload_concurrency)


def finish(name, dry_run=False):
    """Fully publish one episode and append an auditable run record."""
    folder = CONTENT_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    run_report = RunReport(folder, "catalog.finish", {
        "dry_run": dry_run,
        "pages_project": PAGES_PROJECT,
        "r2_bucket": R2_BUCKET,
    })
    try:
        ok = _finish_impl(name, dry_run, run_report)
    except _PublishFailure as exc:
        if not dry_run:
            if load_release(folder):
                update_release_state(folder, "failed", error=exc.error)
            _write_publish_failure(
                name, exc.stage, exc.error, report=exc.report,
                run_id=run_report.run["id"])
        run_report.finish(False, exc.error)
        return False
    except BaseException as exc:
        if not dry_run:
            if load_release(folder):
                update_release_state(folder, "failed", error=exc)
            failed = [s for s in run_report.run.get("stages", []) if s.get("status") == "failed"]
            stage = failed[-1].get("name") if failed else "publish"
            _write_publish_failure(name, stage, exc, run_id=run_report.run["id"])
        run_report.finish(False, exc)
        raise
    run_report.finish(ok, None if ok else "publish transaction failed")
    return ok


def finish_batch(names, dry_run=False, *, upload_concurrency=3):
    """Fully publish a batch and append a run record for every episode."""
    normalized = list(dict.fromkeys(str(name) for name in names if str(name)))
    reports = {}
    for name in normalized:
        folder = CONTENT_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        reports[name] = RunReport(folder, "catalog.finish-batch", {
            "dry_run": dry_run,
            "batch_size": len(normalized),
            "upload_concurrency": int(upload_concurrency),
            "pages_project": PAGES_PROJECT,
            "r2_bucket": R2_BUCKET,
        })
    try:
        ok = _finish_batch_impl(
            normalized, dry_run=dry_run,
            upload_concurrency=upload_concurrency)
    except BaseException as exc:
        for report in reports.values():
            report.finish(False, exc)
        raise
    error = None if ok else "batch publish transaction failed"
    for report in reports.values():
        report.finish(ok, error)
    return ok


# Freeze wrapper identities after every compatibility export exists.
_FACADE_DEFAULTS.update({
    name: value for name, value in list(globals().items())
    if callable(value) and (name.startswith("_") or name in {
        "episode_stats", "rebuild_catalog", "sync_site", "gen_index",
        "catalog_consistency_errors",
    })
})


def main():
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
