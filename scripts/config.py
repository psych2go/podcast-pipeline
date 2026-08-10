"""
Configuration loading via .env / environment variables.

Secrets are loaded lazily: importing the configuration module must not require
TTS or diarization credentials.  Individual pipeline stages call
``require_fish_key`` / ``require_hf_token`` only when they actually need them.
"""
import os
from pathlib import Path

# Project root = parent of scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal .env parser — no external dependency needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            # Do not overwrite an explicitly exported environment variable.
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_ROOT / ".env")


def _require(key: str, hint: str = "") -> str:
    val = os.environ.get(key)
    if not val:
        msg = f"{key} 未设置。请在 {PROJECT_ROOT / '.env'} 中配置"
        if hint:
            msg += f"（{hint}）"
        raise RuntimeError(msg)
    return val


def require_fish_key() -> str:
    return _require("FISH_KEY", "Fish Audio API key, 参考 .env.example")


def require_hf_token() -> str:
    return _require("HF_TOKEN", "HuggingFace token，用于说话人分离")


# Optional values.  Keep the legacy names for callers that import them.
FISH_KEY = os.environ.get("FISH_KEY", "")
FISH_VOICE = os.environ.get("FISH_VOICE", "b561236e80b04f22843c6637682b5478")
FISH_MODEL = os.environ.get("FISH_MODEL", "s2.1-pro-free")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
DS_MODEL = os.environ.get("DS_MODEL", "deepseek-chat")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
BASE_DIR = Path(os.environ.get("PODCAST_DIR", str(PROJECT_ROOT / "content")))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
R2_BUCKET = os.environ.get("R2_BUCKET", "podcast-audio")
PAGES_PROJECT = os.environ.get("PAGES_PROJECT", "podcast-scripts")
PAGES_BASE_URL = os.environ.get(
    "PAGES_BASE_URL", "https://podcast-scripts.pages.dev").rstrip("/")

# ASR configuration.  These are defaults only; CLI arguments take precedence.
ASR_MODEL = os.environ.get("ASR_MODEL", "")
ASR_MODEL_CACHE = os.environ.get("ASR_MODEL_CACHE", "")
ASR_DEVICE = os.environ.get("ASR_DEVICE", "cpu")
ASR_COMPUTE_TYPE = os.environ.get("ASR_COMPUTE_TYPE", "int8")
