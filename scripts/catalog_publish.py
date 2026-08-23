"""Transactional full publication to R2 and Pages with remote verification."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    from atomic_io import atomic_write_json
    import catalog_core as _core_module
    import catalog_site as _site_module
    from catalog_core import (
        CatalogPaths, _catalog_text, _display_title, _find_briefing, _gen_mp3,
        _load_site_entries, _ordered_episode_names, rebuild_catalog,
    )
    from catalog_site import (
        _build_entry, _site_readiness_errors, catalog_consistency_errors,
        gen_index, sync_site,
    )
    from config import (
        PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        validate_for_stage,
    )
    from episode import (
        audio_key as episode_audio_key, page_path as episode_page_path,
        public_audio_url,
    )
    from publish import (
        PUBLISH_REPORT_SCHEMA_VERSION, verify_publish, write_publish_report,
    )
    from publish_errors import RETRYABLE_PAGE_CODES, publish_error_codes
    from quality_report import build_quality_report
    from release import active_audio_key, load_release, update_release_state
    from run_report import RunReport
    from tts import validate_tts_manifest
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts import catalog_core as _core_module
    from scripts import catalog_site as _site_module
    from scripts.catalog_core import (
        CatalogPaths, _catalog_text, _display_title, _find_briefing, _gen_mp3,
        _load_site_entries, _ordered_episode_names, rebuild_catalog,
    )
    from scripts.catalog_site import (
        _build_entry, _site_readiness_errors, catalog_consistency_errors,
        gen_index, sync_site,
    )
    from scripts.config import (
        PAGES_BASE_URL, PAGES_PROJECT, R2_BUCKET, R2_PUBLIC_URL,
        validate_for_stage,
    )
    from scripts.episode import (
        audio_key as episode_audio_key, page_path as episode_page_path,
        public_audio_url,
    )
    from scripts.publish import (
        PUBLISH_REPORT_SCHEMA_VERSION, verify_publish, write_publish_report,
    )
    from scripts.publish_errors import (
        RETRYABLE_PAGE_CODES, publish_error_codes,
    )
    from scripts.quality_report import build_quality_report
    from scripts.release import active_audio_key, load_release, update_release_state
    from scripts.run_report import RunReport
    from scripts.tts import validate_tts_manifest

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
SITE_DIR = BASE_DIR / "site"
CATALOG = CONTENT_DIR / "播客目录.md"


def configure_paths(paths):
    global BASE_DIR, CONTENT_DIR, SITE_DIR, CATALOG
    _core_module.configure_paths(paths)
    _site_module.configure_paths(paths)
    BASE_DIR = Path(paths.base_dir)
    CONTENT_DIR = Path(paths.content_dir)
    SITE_DIR = Path(paths.site_dir)
    CATALOG = Path(paths.catalog)


_DEFAULT_BUILD_QUALITY_REPORT = build_quality_report
_DEFAULT_VALIDATE_TTS_MANIFEST = validate_tts_manifest


def _live_quality_report_builder():
    if build_quality_report is not _DEFAULT_BUILD_QUALITY_REPORT:
        return build_quality_report
    try:
        import quality_report as module
    except ImportError:
        from scripts import quality_report as module
    return module.build_quality_report


def _live_tts_manifest_validator():
    if validate_tts_manifest is not _DEFAULT_VALIDATE_TTS_MANIFEST:
        return validate_tts_manifest
    try:
        import tts as module
    except ImportError:
        from scripts import tts as module
    return module.validate_tts_manifest


def _is_wrangler_command(cmd):
    return any(Path(str(part)).name == "wrangler" for part in cmd)

def _dotenv_assignments(path):
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        values[key] = value
    return values

def _wrangler_environment():
    env = os.environ.copy()
    dotenv = _dotenv_assignments(SITE_DIR.parent / ".env")
    for key in (
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_API_KEY",
            "CLOUDFLARE_EMAIL"):
        # python-dotenv may have loaded a stale project .env value. Remove only
        # that value; preserve credentials explicitly injected by CI/shell.
        if key in dotenv and env.get(key) == dotenv[key]:
            env.pop(key, None)
    return env

def _run_with_output(cmd, cwd, dry_run=False):
    if dry_run:
        print("  [dry-run] " + " ".join(cmd))
        return True, "dry-run"
    try:
        run_cwd = SITE_DIR if _is_wrangler_command(cmd) else cwd
        run_env = (
            _wrangler_environment()
            if _is_wrangler_command(cmd) else None
        )
        r = subprocess.run(
            cmd,
            cwd=run_cwd,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = "\n".join(
            value for value in ((r.stdout or "").strip(), (r.stderr or "").strip())
            if value
        )
        if out:
            print(out[-2000:])
        if r.returncode != 0:
            print(f"[错误] 命令失败 (exit {r.returncode})，已跳过")
            return False, out
        return True, out
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[错误] {type(exc).__name__}: {exc}，已跳过")
        return False, str(exc)

def _run(cmd, cwd, dry_run=False):
    if _is_wrangler_command(cmd):
        return _run_wrangler(cmd, dry_run=dry_run)[0]
    return _run_with_output(cmd, cwd, dry_run=dry_run)[0]

def _wrangler_retryable(output):
    text = str(output or "").casefold()
    return any(marker in text for marker in (
        "fetch failed",
        "connectivity issue",
        "connection reset",
        "connection timed out",
        "network error",
        "timed out",
        "too many requests",
        "status code 429",
        "status code 500",
        "status code 502",
        "status code 503",
        "status code 504",
    ))


def _run_wrangler(cmd, dry_run=False):
    """Run Wrangler with bounded retries for transient network failures."""
    if not _is_wrangler_command(cmd):
        raise ValueError("_run_wrangler 只接受 Wrangler 命令")
    if dry_run:
        return _run_with_output(cmd, SITE_DIR, dry_run=True)
    max_retries = max(0, int(os.environ.get("WRANGLER_MAX_RETRIES", "2")))
    backoff = max(0.0, float(os.environ.get("WRANGLER_RETRY_BACKOFF", "2")))
    result = (False, "")
    for attempt in range(max_retries + 1):
        result = _run_with_output(cmd, SITE_DIR, dry_run=False)
        ok, output = result
        if ok or attempt >= max_retries or not _wrangler_retryable(output):
            return result
        delay = min(30.0, backoff * (2 ** attempt))
        print(
            f"[Wrangler] 网络错误，{delay:g}s 后重试 "
            f"({attempt + 1}/{max_retries})...")
        if delay:
            time.sleep(delay)
    return result

def _verify_publish_with_retry(*args, attempts=4, delay=3):
    """Retry only transient Pages propagation failures after a deployment."""
    report = None
    for attempt in range(1, attempts + 1):
        report = verify_publish(*args)
        if report.get("passed", False):
            return report
        codes = publish_error_codes(report)
        page_only = bool(codes) and all(
            code in RETRYABLE_PAGE_CODES for code in codes)
        if not page_only or attempt == attempts:
            return report
        print(
            f"[发布验证] Pages 可能仍在传播，"
            f"{delay}s 后重试 ({attempt}/{attempts})...")
        time.sleep(delay)
    return report

class _PublishFailure(RuntimeError):
    def __init__(self, stage, error, report=None):
        self.stage = stage
        self.error = str(error) or type(error).__name__
        self.report = report
        super().__init__(self.error)

def _release_report(folder, audio_key=None):
    release = load_release(folder)
    if not release:
        return {
            "release_id": "",
            "audio_key": audio_key or "",
            "state": "untracked",
            "last_successful_state": "",
            "previous_release_id": "",
        }
    return {
        "release_id": release.get("release_id", ""),
        "audio_key": release.get("audio_key") or audio_key or "",
        "state": release.get("state", ""),
        "last_successful_state": release.get("last_successful_state", ""),
        "previous_release_id": release.get("previous_release_id", ""),
    }

def _write_publish_failure(
        name,
        failed_stage,
        error,
        *,
        report=None,
        run_id="",
        audio_key=None,
):
    folder = CONTENT_DIR / name
    error_text = str(error) or type(error).__name__
    payload = dict(report or {})
    existing_errors = payload.get("errors")
    if isinstance(existing_errors, list):
        errors = list(existing_errors)
    elif existing_errors:
        errors = [str(existing_errors)]
    else:
        errors = []
    if error_text not in errors:
        errors.append(error_text)
    if not audio_key:
        try:
            audio_key = active_audio_key(
                folder, episode_audio_key(folder))
        except Exception:
            audio_key = ""
    payload.update({
        "schema_version": PUBLISH_REPORT_SCHEMA_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "errors": errors,
        "failed_stage": failed_stage,
        "error": error_text,
        "release": _release_report(folder, audio_key),
    })
    if run_id:
        payload["run_id"] = run_id
    write_publish_report(folder / "publish_report.json", payload)
    return payload

def _candidate_catalog_errors(name):
    """Validate the site/catalog candidate without writing generated files."""
    entries = _load_site_entries()
    existing = {
        entry.get("folder"): entry
        for entry in entries
        if entry.get("folder")
    }
    names = _ordered_episode_names()
    errors = []
    if name not in names:
        errors.append(f"{name}: 候选目录中找不到目标单集")
        return errors

    errors.extend(_site_readiness_errors(
        names, existing, strict_names={name}))
    candidate_entries = [
        _build_entry(episode_name, existing.get(episode_name, {}))
        for episode_name in names
    ]
    if [entry.get("folder") for entry in candidate_entries] != names:
        errors.append("候选 site.json 顺序或单集集合不一致")

    paths = {}
    for entry in candidate_entries:
        path = entry.get("path")
        if not path:
            errors.append(f"{entry.get('folder')}: 候选页面路径为空")
            continue
        if path in paths:
            errors.append(
                f"候选页面路径冲突: {paths[path]} 与 "
                f"{entry.get('folder')} 均使用 {path}"
            )
        paths[path] = entry.get("folder")

    try:
        _catalog_text(names)
    except Exception as exc:
        errors.append(f"候选播客目录生成失败: {exc}")
    return errors

def _publish_preflight(name):
    """发布前验证内容质量、产物存在性、新鲜度和音频可解码性。"""
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        print(f"[发布前检查][阻断] 找不到播客目录: {folder}")
        return False

    report = _live_quality_report_builder()(folder)
    atomic_write_json(folder / "quality_report.json", report)
    if not report.get("passed", False):
        for error in report.get("errors", []):
            print(f"[发布前检查][阻断] {error}")
        return False

    briefing = _find_briefing(folder)
    mp3 = _gen_mp3(folder)
    html = folder / f"{name} - content.html"
    if not briefing or not mp3 or not html.exists():
        print("[发布前检查][阻断] 缺少讲稿、最终 MP3 或 content.html")
        return False

    release = load_release(folder)
    if release:
        audio_sha256 = hashlib.sha256(mp3.read_bytes()).hexdigest()
        briefing_sha256 = hashlib.sha256(briefing.read_bytes()).hexdigest()
        if release.get("audio_sha256") != audio_sha256:
            print("[发布前检查][阻断] release.json 音频哈希已过期")
            return False
        if release.get("briefing_sha256") != briefing_sha256:
            print("[发布前检查][阻断] release.json 讲稿哈希已过期")
            return False

    briefing_sha256 = hashlib.sha256(briefing.read_bytes()).hexdigest()
    html_text = html.read_text(encoding="utf-8")
    html_hash_match = re.search(
        r'<meta name="podcast-source-sha256" content="([0-9a-f]{64})">',
        html_text,
    )
    if not html_hash_match or html_hash_match.group(1) != briefing_sha256:
        print("[发布前检查][阻断] content.html 未绑定当前讲稿哈希，必须重新生成")
        return False
    if release and release.get("audio_key") not in html.read_text(
            encoding="utf-8"):
        print("[发布前检查][阻断] content.html 未绑定当前 release 音频 key")
        return False

    tts_errors = _live_tts_manifest_validator()(
        folder, briefing.name, name)
    if tts_errors:
        for error in tts_errors:
            print(f"[发布前检查][阻断] {error}")
        return False

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,duration",
                "-of", "json", str(mp3),
            ],
            capture_output=True, text=True, timeout=30,
        )
        payload = json.loads(probe.stdout or "{}")
        streams = payload.get("streams", [])
        if probe.returncode != 0 or not streams or streams[0].get("codec_name") != "mp3":
            print("[发布前检查][阻断] 最终音频无法通过 ffprobe 解码")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"[发布前检查][阻断] 音频检查失败: {exc}")
        return False

    print("[发布前检查] 质量、哈希、产物新鲜度和音频格式均通过")
    return True

def _finish_impl(name, dry_run, run_report):
    print(f"=== 一键收尾: {name} ===")
    folder = CONTENT_DIR / name
    with run_report.stage("config_preflight"):
        validate_for_stage("publish", dry_run=dry_run)
    with run_report.stage("publish_preflight") as stage:
        preflight_ok = _publish_preflight(name)
        stage.metrics["passed"] = preflight_ok
        if not preflight_ok:
            raise _PublishFailure(
                "publish_preflight", "publish preflight failed")

    release = load_release(folder)
    if release and not dry_run:
        release = update_release_state(folder, "prepared")
    mp3 = _gen_mp3(folder)
    key = active_audio_key(
        folder,
        episode_audio_key(folder),
    )

    upload_item = {
        "name": name,
        "folder": folder,
        "mp3": mp3,
        "release": release,
        "key": key,
    }

    if dry_run:
        with run_report.stage("dry_run_catalog") as stage:
            candidate_errors = _candidate_catalog_errors(name)
            stage.metrics["error_count"] = len(candidate_errors)
            stage.metrics["candidate_episode_count"] = len(
                _ordered_episode_names())
            if candidate_errors:
                for error in candidate_errors:
                    print(f"[dry-run][阻断] {error}")
                raise _PublishFailure(
                    "dry_run_catalog", "; ".join(candidate_errors))
        with run_report.stage("upload_r2", {
                "dry_run": True,
                "size_bytes": mp3.stat().st_size,
                "release_id": release.get("release_id") if release else "",
        }) as stage:
            _upload_r2_item(upload_item, dry_run=True)
            stage.metrics["object_key"] = key
        with run_report.stage("deploy_pages", {
                "dry_run": True,
                "project": PAGES_PROJECT,
        }):
            _run_wrangler(
                ["npx", "wrangler", "pages", "deploy", ".",
                 "--project-name", PAGES_PROJECT, "--branch", "main"],
                dry_run=True,
            )
        print("[完成] dry-run：未修改 site、台账、首页或 release 状态")
        return True

    with run_report.stage("upload_r2", {
            "dry_run": False,
            "size_bytes": mp3.stat().st_size,
            "release_id": release.get("release_id") if release else "",
    }) as stage:
        uploaded = _upload_r2_item(upload_item, dry_run=False)
        stage.metrics["object_key"] = key
        if not uploaded:
            print("[发布] R2 上传失败，停止 Pages 部署")
            raise _PublishFailure("upload_r2", "R2 upload failed")
    if release:
        release = update_release_state(folder, "uploaded")

    with run_report.stage("sync_site") as stage:
        try:
            sync_site(only=name)
        except SystemExit as exc:
            raise _PublishFailure("sync_site", exc) from exc
        stage.metrics["episode_count"] = len(_load_site_entries())

    with run_report.stage("rebuild_catalog") as stage:
        try:
            names = rebuild_catalog()
        except SystemExit as exc:
            raise _PublishFailure("rebuild_catalog", exc) from exc
        stage.metrics["episode_count"] = len(names)

    with run_report.stage("generate_index"):
        try:
            gen_index()
        except SystemExit as exc:
            raise _PublishFailure("generate_index", exc) from exc

    with run_report.stage("catalog_consistency") as stage:
        consistency_errors = catalog_consistency_errors()
        stage.metrics["error_count"] = len(consistency_errors)
        if consistency_errors:
            for error in consistency_errors:
                print(f"[一致性][阻断] {error}")
            raise _PublishFailure(
                "catalog_consistency", "; ".join(consistency_errors))
    if release:
        release = update_release_state(folder, "site_ready")

    with run_report.stage("deploy_pages", {
            "dry_run": False,
            "project": PAGES_PROJECT,
    }) as stage:
        print("[部署] Pages 部署 ...")
        deployed, deploy_output = _run_wrangler(
            ["npx", "wrangler", "pages", "deploy", ".",
             "--project-name", PAGES_PROJECT, "--branch", "main"],
            dry_run=False,
        )
        if not deployed:
            print("[发布] Pages 部署失败")
            raise _PublishFailure(
                "deploy_pages", "Pages deployment failed")
    if release:
        release = update_release_state(folder, "deployed")

    if not R2_PUBLIC_URL:
        print("[发布][阻断] R2_PUBLIC_URL 未配置，无法验证线上音频")
        raise _PublishFailure(
            "verify_publish", "R2_PUBLIC_URL 未配置")

    display_title = _display_title(name)
    public_path = quote(
        episode_page_path(folder), safe="")
    episode_url = f"{PAGES_BASE_URL}/{public_path}/content.html"
    audio_url = public_audio_url(folder, R2_PUBLIC_URL)
    with run_report.stage("verify_publish") as stage:
        report = _verify_publish_with_retry(
            PAGES_BASE_URL + "/",
            episode_url,
            audio_url,
            display_title,
            mp3,
        )
        stage.metrics["passed"] = bool(report.get("passed"))
        stage.metrics["error_count"] = len(report.get("errors", []))
        stage.metrics["statuses"] = {
            check: values.get("status")
            for check, values in report.get("checks", {}).items()
        }
        if not report.get("passed", False):
            raise _PublishFailure(
                "verify_publish",
                "; ".join(report.get("errors", []))
                or "remote verification failed",
                report=report,
            )
    deployment_match = re.search(
        r"https://[A-Za-z0-9-]+\." + re.escape(PAGES_PROJECT) + r"\.pages\.dev",
        deploy_output,
    )
    if deployment_match:
        report["deployment_url"] = deployment_match.group(0)
    report["stable_url"] = PAGES_BASE_URL
    if release:
        report["release"] = {
            **_release_report(folder, key),
            "state": "published",
            "last_successful_state": "published",
        }
    report["run_id"] = run_report.run["id"]
    report_path = folder / "publish_report.json"
    write_publish_report(report_path, report)
    if release:
        update_release_state(folder, "published")
    print(f"[发布验证] Pages、R2 和 Range 均通过 → {report_path.name}")
    print("[完成] 台账、site、首页、R2、Pages 已处理")
    return True

def _batch_publish_item(name):
    folder = CONTENT_DIR / name
    mp3 = _gen_mp3(folder)
    release = load_release(folder)
    return {
        "name": name,
        "folder": folder,
        "mp3": mp3,
        "release": release,
        "key": active_audio_key(folder, episode_audio_key(folder)),
    }

def _upload_r2_item(item, dry_run=False):
    print(f"[R2] 上传 {item['mp3'].name} 到 R2 ...")
    return _run(
        [
            "npx", "wrangler", "r2", "object", "put",
            f"{R2_BUCKET}/{item['key']}",
            "--file", str(item["mp3"]),
            "--content-type", "audio/mpeg", "--remote",
        ],
        cwd=BASE_DIR,
        dry_run=dry_run,
    )

def _finish_batch_impl(names, dry_run=False, *, upload_concurrency=3):
    """Fully publish multiple episodes with parallel R2 upload and one Pages deploy."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    names = list(dict.fromkeys(str(name) for name in names if str(name)))
    if not names:
        raise ValueError("finish-batch 至少需要一个单集名称")
    upload_concurrency = int(upload_concurrency)
    if upload_concurrency < 1:
        raise ValueError("upload_concurrency 必须大于等于 1")

    validate_for_stage("publish", dry_run=dry_run)
    for name in names:
        if not _publish_preflight(name):
            if not dry_run:
                folder = CONTENT_DIR / name
                if load_release(folder):
                    update_release_state(
                        folder, "failed", error="publish preflight failed")
                _write_publish_failure(
                    name, "publish_preflight", "publish preflight failed")
            return False

    candidate_errors_by_name = {
        name: _candidate_catalog_errors(name)
        for name in names
    }
    candidate_errors = [
        f"{name}: {error}"
        for name, errors in candidate_errors_by_name.items()
        for error in errors
    ]
    if candidate_errors:
        for error in candidate_errors:
            print(f"[finish-batch][阻断] {error}")
        if not dry_run:
            for name in names:
                folder = CONTENT_DIR / name
                if load_release(folder):
                    update_release_state(
                        folder, "failed", error="; ".join(candidate_errors))
                _write_publish_failure(
                    name,
                    "catalog_candidate",
                    "; ".join(candidate_errors),
                )
        return False

    items = [_batch_publish_item(name) for name in names]
    if any(item["mp3"] is None for item in items):
        missing = [
            item["name"] for item in items if item["mp3"] is None]
        raise RuntimeError(f"批量发布缺少最终 MP3: {missing}")

    if dry_run:
        for item in items:
            _upload_r2_item(item, dry_run=True)
        _run_wrangler(
            [
                "npx", "wrangler", "pages", "deploy", ".",
                "--project-name", PAGES_PROJECT, "--branch", "main",
            ],
            dry_run=True,
        )
        print(
            f"[完成] finish-batch dry-run：{len(items)} 期完整发布；"
            "未修改 site、台账、首页或 release 状态")
        return True

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "prepared")

    upload_results = {}
    workers = max(1, min(upload_concurrency, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_upload_r2_item, item, False): item
            for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                upload_results[item["name"]] = bool(future.result())
            except Exception as exc:
                print(f"[R2][错误] {item['name']}: {exc}")
                upload_results[item["name"]] = False

    failed_uploads = [
        item for item in items
        if not upload_results.get(item["name"], False)
    ]
    for item in items:
        if item in failed_uploads:
            error = "R2 upload failed"
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "upload_r2", error,
                audio_key=item["key"])
        elif item["release"]:
            item["release"] = update_release_state(
                item["folder"], "uploaded")
    if failed_uploads:
        return False

    try:
        sync_site(strict_names={item["name"] for item in items})
        rebuild_catalog()
        gen_index()
    except SystemExit as exc:
        error = str(exc) or "site generation failed"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "sync_site", error,
                audio_key=item["key"])
        return False

    consistency_errors = catalog_consistency_errors()
    if consistency_errors:
        error = "; ".join(consistency_errors)
        for detail in consistency_errors:
            print(f"[一致性][阻断] {detail}")
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "catalog_consistency", error,
                audio_key=item["key"])
        return False

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "site_ready")

    print(f"[部署] Pages 批量部署 {len(items)} 期 ...")
    deployed, deploy_output = _run_wrangler(
        [
            "npx", "wrangler", "pages", "deploy", ".",
            "--project-name", PAGES_PROJECT, "--branch", "main",
        ],
        dry_run=False,
    )
    if not deployed:
        error = "Pages deployment failed"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "deploy_pages", error,
                audio_key=item["key"])
        return False

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "deployed")

    if not R2_PUBLIC_URL:
        error = "R2_PUBLIC_URL 未配置"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "verify_publish", error,
                audio_key=item["key"])
        return False

    deployment_match = re.search(
        r"https://[A-Za-z0-9-]+\." + re.escape(PAGES_PROJECT)
        + r"\.pages\.dev",
        deploy_output,
    )
    all_passed = True
    for item in items:
        display_title = _display_title(item["name"])
        public_path = quote(
            episode_page_path(item["folder"]), safe="")
        episode_url = f"{PAGES_BASE_URL}/{public_path}/content.html"
        audio_url = public_audio_url(item["folder"], R2_PUBLIC_URL)
        report = _verify_publish_with_retry(
            PAGES_BASE_URL + "/",
            episode_url,
            audio_url,
            display_title,
            item["mp3"],
        )
        report["stable_url"] = PAGES_BASE_URL
        report["batch_size"] = len(items)
        if deployment_match:
            report["deployment_url"] = deployment_match.group(0)
        if report.get("passed", False):
            if item["release"]:
                report["release"] = {
                    **_release_report(item["folder"], item["key"]),
                    "state": "published",
                    "last_successful_state": "published",
                }
                update_release_state(item["folder"], "published")
            write_publish_report(
                item["folder"] / "publish_report.json", report)
            print(
                f"[发布验证] {item['name']}: Pages、R2 和 Range 均通过")
            continue

        all_passed = False
        error = (
            "; ".join(report.get("errors", []))
            or "remote verification failed"
        )
        if item["release"]:
            update_release_state(item["folder"], "failed", error=error)
            report["release"] = _release_report(
                item["folder"], item["key"])
        report["failed_stage"] = "verify_publish"
        report["error"] = error
        write_publish_report(
            item["folder"] / "publish_report.json", report)

    if all_passed:
        print(
            f"[完成] {len(items)} 期音频上传、一次 Pages 部署和逐期验收已完成")
    return all_passed

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
            failed = [
                stage for stage in run_report.run.get("stages", [])
                if stage.get("status") == "failed"
            ]
            stage = failed[-1].get("name") if failed else "publish"
            _write_publish_failure(
                name, stage, exc, run_id=run_report.run["id"])
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
