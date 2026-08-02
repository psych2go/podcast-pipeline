"""
Configuration loading via .env / environment variables.
Uses a minimal inline parser instead of python-dotenv to avoid external dependencies.
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
            os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(PROJECT_ROOT / ".env")


def _require(key: str, hint: str = "") -> str:
    val = os.environ.get(key)
    if not val:
        msg = f"{key} 未设置。请在 {PROJECT_ROOT / '.env'} 中配置"
        if hint:
            msg += f"（{hint}）"
        raise RuntimeError(msg)
    return val


# --- Required ---
# v4 起讲稿生成由 Claude Code 终端直接完成，不再需要 DeepSeek；唯一必需的 key 是 Fish Audio。
FISH_KEY = _require("FISH_KEY", "Fish Audio API key, 参考 .env.example")

# --- Optional with defaults ---
# legacy: 讲稿生成已改由 Claude 直接完成，下列 DeepSeek 相关变量仅供 legacy/generator.py 回溯使用
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
FISH_VOICE = os.environ.get("FISH_VOICE", "b561236e80b04f22843c6637682b5478")
FISH_MODEL = os.environ.get("FISH_MODEL", "s2.1-pro-free")
DS_MODEL = os.environ.get("DS_MODEL", "deepseek-chat")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
BASE_DIR = Path(
    os.environ.get("PODCAST_DIR", str(PROJECT_ROOT / "content"))
)
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# R2 桶公开访问地址（如 https://pub-xxxxxxxx.r2.dev）。
# 播放器用它拼 mp3 绝对 URL（支持 Range 流式分片）；为空则回退到相对路径 {播客名}.mp3
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
