"""Create the isolated lightweight WhisperX alignment environment."""
import argparse
import subprocess
import sys
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = PROJECT_ROOT / ".venv-alignment"
MAIN_SITE = (
    PROJECT_ROOT / ".venv" / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)


def run(command):
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="创建隔离 WhisperX forced-alignment 环境")
    parser.add_argument("--env", default=str(DEFAULT_ENV))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    target = Path(args.env)
    python = target / "bin" / "python"
    if args.force or not python.exists():
        venv.EnvBuilder(with_pip=True, clear=args.force).create(target)
    python = target / "bin" / "python"
    pip = target / "bin" / "pip"
    run([
        str(pip), "install", "--upgrade",
        "pip", "setuptools", "wheel",
    ])
    run([
        str(pip), "install", "-r",
        str(PROJECT_ROOT / "requirements-alignment.txt"),
    ])
    run([
        str(pip), "install", "--no-deps", "whisperx==3.8.6",
    ])

    run([
        str(python), "-m", "nltk.downloader", "punkt_tab",
    ])

    output = subprocess.check_output(
        [
            str(python), "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        text=True,
    ).strip()
    alignment_site = Path(output)
    alignment_site.mkdir(parents=True, exist_ok=True)
    (alignment_site / "podcast_pipeline_main_venv.pth").write_text(
        str(MAIN_SITE.resolve()) + "\n",
        encoding="utf-8",
    )
    run([
        str(python), "-c",
        (
            "import whisperx; "
            "from whisperx.alignment import load_align_model, align; "
            "print('alignment environment ready')"
        ),
    ])
    print(f"[完成] alignment Python: {python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
