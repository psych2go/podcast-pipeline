"""Generated site manifest, page synchronization, and consistency checks."""
import json
import os
import sys
from pathlib import Path

try:
    from atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    from episode import (legacy_page_path as episode_legacy_page_path, page_path as episode_page_path, quality_metadata)
    from site_index import render_index
except ImportError:
    from scripts.atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    from scripts.episode import (legacy_page_path as episode_legacy_page_path, page_path as episode_page_path, quality_metadata)
    from scripts.site_index import render_index

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
SITE_DIR = BASE_DIR / "site"
CATALOG = CONTENT_DIR / "播客目录.md"
PUBLIC_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
PUBLIC_ASSET_SUFFIXES = {".avif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}

# Injected by the catalog facade; defaults keep direct module imports usable.
try:
    import catalog_core as _core_module
    from catalog_core import (
        CatalogPaths, _catalog_text, _display_title, _episode_dirs,
        _find_briefing, _gen_mp3, _load_site_entries,
        _ordered_episode_names, _read_source, episode_stats,
    )
except ImportError:
    from scripts import catalog_core as _core_module
    from scripts.catalog_core import (
        CatalogPaths, _catalog_text, _display_title, _episode_dirs,
        _find_briefing, _gen_mp3, _load_site_entries,
        _ordered_episode_names, _read_source, episode_stats,
    )


def configure_paths(paths):
    global BASE_DIR, CONTENT_DIR, SITE_DIR, CATALOG
    _core_module.configure_paths(paths)
    BASE_DIR = Path(paths.base_dir)
    CONTENT_DIR = Path(paths.content_dir)
    SITE_DIR = Path(paths.site_dir)
    CATALOG = Path(paths.catalog)
def _build_entry(name, prev):
    folder = CONTENT_DIR / name
    stats = episode_stats(name)
    url, label = _read_source(name)
    path = episode_page_path(folder)
    return {
        "title": _display_title(name),
        "folder": name,
        "path": path,
        "slug": path,
        "duration": stats["duration"],
        "words": stats["chars"],
        "source_name": label or prev.get("source_name") or "",
        "source_url": url or prev.get("source_url") or "",
        "quality_mode": quality_metadata(folder).get(
            "mode",
            "strict" if (folder / "content_map.json").exists() else "legacy",
        ),
    }

def _site_readiness_errors(names, existing, strict_names=None):
    errors = []
    strict_names = set(names if strict_names is None else strict_names)
    try:
        from quality_report import build_quality_report
    except ImportError:
        from scripts.quality_report import build_quality_report

    for name in names:
        folder = CONTENT_DIR / name
        mp3 = _gen_mp3(folder)
        html = folder / f"{name} - content.html"
        if not mp3:
            errors.append(f"{name}: 缺少最终 MP3")
        if not html.exists():
            errors.append(f"{name}: 缺少 content.html")

        content_map = folder / "content_map.json"
        if content_map.exists() and (
                name in strict_names or name not in existing):
            report = build_quality_report(folder, strict=True)
            if not report.get("passed", False):
                detail = "; ".join(report.get("errors", [])[:3])
                errors.append(f"{name}: 严格质量门未通过: {detail}")
        elif name not in existing:
            errors.append(
                f"{name}: 新期缺少 content_map.json，不能进入站点")
    return errors

def sync_site(only=None, *, strict_names=None):
    """重建 site.json（全部期），并按需拷贝 content.html 到 site/{name}/。

    保留已有 site.json 里手工精修的 title / source 字段与出现顺序，
    只更新字数/时长，新期追加在末尾。
    --only 只限制 content.html 的拷贝范围，site.json 始终重建全部期。
    """
    existing = {}
    site_json_path = SITE_DIR / "site.json"
    if site_json_path.exists():
        try:
            for e in json.loads(site_json_path.read_text(encoding="utf-8")):
                existing[e.get("folder")] = e
        except Exception:
            existing = {}

    names = _ordered_episode_names()
    if only and only not in names:
        sys.exit(f"[错误] 找不到播客目录（或没有讲稿）: {only}")
    if strict_names is None:
        strict_names = {only} if only else set(names)
    readiness_errors = _site_readiness_errors(
        names, existing, strict_names=strict_names)
    if readiness_errors:
        for error in readiness_errors:
            print(f"[站点][阻断] {error}")
        sys.exit("[站点][阻断] 存在未就绪单集，未修改 site/")

    # 拷贝 content.html（--only 只影响这一步）；音频由 R2 公开 URL 提供，不放进 site/
    for name in names:
        if only and name != only:
            continue
        folder = CONTENT_DIR / name
        html = folder / f"{name} - content.html"
        if html.exists():
            paths = {
                episode_page_path(folder),
                episode_legacy_page_path(folder),
            } - {""}
            for public_path in paths:
                destination = SITE_DIR / public_path
                destination.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(
                    destination / "content.html", html.read_bytes())

    # 重建 site.json（始终覆盖全部期）
    eps = [_build_entry(name, existing.get(name, {})) for name in names]

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    asset_target = SITE_DIR / "assets"
    if PUBLIC_ASSET_DIR.exists():
        asset_target.mkdir(parents=True, exist_ok=True)
        for asset in PUBLIC_ASSET_DIR.iterdir():
            if asset.is_file() and asset.suffix.lower() in PUBLIC_ASSET_SUFFIXES:
                atomic_write_bytes(
                    asset_target / asset.name, asset.read_bytes())
    atomic_write_json(site_json_path, eps)
    copied = len(names) if only is None else (1 if only in names else 0)
    print(f"[站点] {len(eps)} 期 → {site_json_path.name}（content.html 已同步 {copied} 期）")

def catalog_consistency_errors():
    """验证台账和 site.json 都与当前内容统计一致。"""
    names = _ordered_episode_names()
    errors = []
    try:
        actual_catalog = CATALOG.read_text(encoding="utf-8")
    except OSError:
        actual_catalog = ""
    expected_catalog = _catalog_text(names)
    if actual_catalog != expected_catalog:
        errors.append("播客目录.md 与当前单集统计不一致，需运行 catalog.py rebuild")

    entries = _load_site_entries()
    entry_names = [entry.get("folder") for entry in entries]
    if entry_names != names:
        errors.append("site.json 顺序或单集集合与播客台账不一致")
        return errors
    for name, entry in zip(names, entries):
        expected = _build_entry(name, entry)
        for field in (
                "title", "path", "slug", "duration", "words",
                "source_name", "source_url", "quality_mode"):
            if entry.get(field) != expected.get(field):
                errors.append(
                    f"{name}: site.json.{field}="
                    f"{entry.get(field)!r}，当前应为 {expected.get(field)!r}"
                )
    return errors

def gen_index():
    """从 site.json 重建首页；具体渲染由 site_index 深模块负责。"""
    try:
        result = render_index(SITE_DIR)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"[错误] {exc}")
    print(
        f"[首页] {result['episode_count']} 期卡片 + 统计已更新 "
        f"→ {result['index_path']}"
    )

def backfill_sources():
    """为缺少 来源.md 的期，从 site.json 回填（标题 + 来源链接）。"""
    site_json_path = SITE_DIR / "site.json"
    if not site_json_path.exists():
        sys.exit("[错误] 找不到 site/site.json")
    entries = json.loads(site_json_path.read_text(encoding="utf-8"))
    written = skipped = 0
    for e in entries:
        name = e["folder"]
        url = e.get("source_url")
        folder = CONTENT_DIR / name
        if not folder.is_dir():
            continue
        src_path = folder / "来源.md"
        if src_path.exists():
            skipped += 1
            continue
        if not url:
            print(f"[来源] 跳过 {name}（无 source_url）")
            skipped += 1
            continue
        md = _find_briefing(folder)
        if md:
            import time as _time
            d = _time.strftime("%Y-%m-%d", _time.localtime(os.path.getmtime(md)))
        else:
            d = "未知"
        atomic_write_text(
            src_path,
            f"# 来源信息\n\n## 原始播客\n- 标题：{name}\n\n## 转录来源\n"
            f"- 链接：{url}\n- 转录来源：{url}\n\n## 处理信息\n"
            f"- 处理日期：{d}\n",
        )
        written += 1
        print(f"[来源] 已回填 {name}")
    print(f"[来源] 回填 {written} 期，跳过 {skipped} 期")
