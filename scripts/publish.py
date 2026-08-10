"""Cloudflare Pages and R2 publish verification."""
import html
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx


PUBLISH_REPORT_SCHEMA_VERSION = 1


def _response_summary(response):
    return {
        "status": response.status_code,
        "url": str(response.url),
        "content_type": response.headers.get("content-type", ""),
    }


def verify_publish(
        homepage_url,
        episode_url,
        audio_url,
        display_title,
        local_mp3,
        client=None,
):
    """Verify the stable Pages site and public R2 audio object."""
    report = {
        "schema_version": PUBLISH_REPORT_SCHEMA_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "errors": [],
        "checks": {},
    }
    local_mp3 = Path(local_mp3)
    if not local_mp3.exists():
        report["errors"].append(f"本地音频不存在: {local_mp3}")
        report["passed"] = False
        return report

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30, connect=15),
            headers={"User-Agent": "podcast-pipeline-publish-check/1"},
        )

    try:
        homepage = client.get(homepage_url)
        homepage_check = _response_summary(homepage)
        homepage_text = html.unescape(homepage.text)
        homepage_check["title_present"] = display_title in homepage_text
        report["checks"]["homepage"] = homepage_check
        if homepage.status_code != 200:
            report["errors"].append(
                f"首页状态异常: {homepage.status_code}")
        if not homepage_check["title_present"]:
            report["errors"].append("首页未找到目标单集标题")

        episode = client.get(episode_url)
        episode_check = _response_summary(episode)
        episode_text = html.unescape(episode.text)
        episode_check["title_present"] = display_title in episode_text
        episode_check["player_present"] = 'id="podcastAudio"' in episode.text
        report["checks"]["episode"] = episode_check
        if episode.status_code != 200:
            report["errors"].append(
                f"单期页面状态异常: {episode.status_code}")
        if not episode_check["title_present"]:
            report["errors"].append("单期页面未找到展示标题")
        if not episode_check["player_present"]:
            report["errors"].append("单期页面缺少音频播放器")

        audio_head = client.head(audio_url)
        head_check = _response_summary(audio_head)
        head_check["content_length"] = int(
            audio_head.headers.get("content-length", "0") or 0)
        head_check["accept_ranges"] = audio_head.headers.get(
            "accept-ranges", "")
        head_check["local_size"] = local_mp3.stat().st_size
        report["checks"]["audio_head"] = head_check
        if audio_head.status_code != 200:
            report["errors"].append(
                f"R2 音频 HEAD 状态异常: {audio_head.status_code}")
        if "audio/mpeg" not in head_check["content_type"].lower():
            report["errors"].append(
                f"R2 音频类型异常: {head_check['content_type']}")
        if head_check["content_length"] != head_check["local_size"]:
            report["errors"].append(
                "R2 音频大小与本地文件不一致: "
                f"{head_check['content_length']} != {head_check['local_size']}"
            )
        if head_check["accept_ranges"].lower() != "bytes":
            report["errors"].append("R2 音频未声明 Accept-Ranges: bytes")

        audio_range = client.get(
            audio_url, headers={"Range": "bytes=0-1023"})
        range_check = _response_summary(audio_range)
        range_check["bytes"] = len(audio_range.content)
        range_check["content_range"] = audio_range.headers.get(
            "content-range", "")
        report["checks"]["audio_range"] = range_check
        if audio_range.status_code != 206:
            report["errors"].append(
                f"R2 Range 状态异常: {audio_range.status_code}")
        if len(audio_range.content) != 1024:
            report["errors"].append(
                f"R2 Range 返回长度异常: {len(audio_range.content)}")
        expected_range = f"bytes 0-1023/{local_mp3.stat().st_size}"
        if range_check["content_range"] != expected_range:
            report["errors"].append(
                "R2 Content-Range 异常: "
                f"{range_check['content_range']!r} != {expected_range!r}"
            )
    except httpx.HTTPError as exc:
        report["errors"].append(f"线上验证请求失败: {exc}")
    finally:
        if owns_client:
            client.close()

    report["passed"] = not report["errors"]
    return report


def write_publish_report(path, report):
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
