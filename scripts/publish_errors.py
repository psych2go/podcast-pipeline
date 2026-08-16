"""Stable error codes for remote Pages/R2 verification."""

LOCAL_AUDIO_MISSING = "publish_local_audio_missing"
HOMEPAGE_STATUS = "publish_homepage_status"
HOMEPAGE_EPISODE_MISSING = "publish_homepage_episode_missing"
EPISODE_STATUS = "publish_episode_status"
EPISODE_TITLE_MISSING = "publish_episode_title_missing"
EPISODE_PLAYER_MISSING = "publish_episode_player_missing"
AUDIO_HEAD_STATUS = "publish_audio_head_status"
AUDIO_CONTENT_TYPE = "publish_audio_content_type"
AUDIO_SIZE_MISMATCH = "publish_audio_size_mismatch"
AUDIO_RANGE_HEADER = "publish_audio_range_header"
AUDIO_RANGE_STATUS = "publish_audio_range_status"
AUDIO_RANGE_LENGTH = "publish_audio_range_length"
AUDIO_CONTENT_RANGE = "publish_audio_content_range"
REQUEST_FAILED = "publish_request_failed"

RETRYABLE_PAGE_CODES = frozenset({
    HOMEPAGE_STATUS,
    HOMEPAGE_EPISODE_MISSING,
    EPISODE_STATUS,
    EPISODE_TITLE_MISSING,
    EPISODE_PLAYER_MISSING,
})


def add_publish_error(report, code, message):
    text = str(message)
    report.setdefault("errors", []).append(text)
    report.setdefault("error_details", []).append({
        "code": str(code),
        "message": text,
    })


def publish_error_codes(report):
    errors = [str(item) for item in report.get("errors", [])]
    details = report.get("error_details")
    if not isinstance(details, list) or len(details) != len(errors):
        return []
    messages = []
    codes = []
    for item in details:
        if not isinstance(item, dict) or not item.get("code"):
            return []
        messages.append(str(item.get("message")))
        codes.append(str(item["code"]))
    return codes if messages == errors else []
