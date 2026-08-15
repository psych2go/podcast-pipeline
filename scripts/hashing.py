"""Canonical SHA-256 helpers for pipeline files, text, and byte payloads."""

import hashlib
from pathlib import Path


BLOCK_SIZE = 1024 * 1024


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str, *, strip=False) -> str:
    text = str(value or "")
    if strip:
        text = text.strip()
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()
