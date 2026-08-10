"""Shared source URL display labels."""
from urllib.parse import urlsplit


SOURCE_LABELS = {
    "podcasts.happyscribe.com": "HappyScribe",
    "happyscribe.com": "HappyScribe",
    "nav.al": "nav.al",
    "singjupost.com": "SingjuPost",
    "podscripts.co": "podscripts.co",
}


def source_host(url):
    candidate = url if "://" in (url or "") else f"//{url or ''}"
    host = (urlsplit(candidate).hostname or "").lower()
    return host.removeprefix("www.")


def source_label(url):
    host = source_host(url)
    return SOURCE_LABELS.get(host, host)
