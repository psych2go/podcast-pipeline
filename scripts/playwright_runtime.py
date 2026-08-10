import os
import platform
from pathlib import Path


_ARCH_TRIPLETS = {
    "aarch64": "aarch64-linux-gnu",
    "arm64": "aarch64-linux-gnu",
    "x86_64": "x86_64-linux-gnu",
}


def playwright_launch_env():
    """Return a browser environment with optional user-installed system libs."""
    env = os.environ.copy()
    library_path = env.get("PLAYWRIGHT_LIBRARY_PATH")

    if not library_path:
        os_release = platform.freedesktop_os_release()
        distro = os_release.get("ID", "")
        version = os_release.get("VERSION_ID", "")
        triplet = _ARCH_TRIPLETS.get(platform.machine())
        if distro and version and triplet:
            candidate = (
                Path.home()
                / ".local"
                / "share"
                / "playwright-system-deps"
                / f"{distro}-{version}"
                / "usr"
                / "lib"
                / triplet
            )
            if candidate.is_dir():
                library_path = str(candidate)

    if library_path:
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = (
            f"{library_path}{os.pathsep}{existing}"
            if existing else library_path
        )

    return env
