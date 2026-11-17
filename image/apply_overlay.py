"""Lay the feature files and the frontend wheel over the installed Music Assistant server.

Runs inside the image build, with the server's own Python. The context directory holds:

- overlay/music_assistant/...: the package files the feature adds or changes
- deleted.txt: package paths the feature removes, one per line (music_assistant/...)
- music_assistant_frontend-*.whl: the frontend wheel to install in place of the pinned one
"""

from __future__ import annotations

import compileall
import os
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path

BUILD_INFO = Path("/app/build_info.json")


def main(context: Path) -> None:
    site = Path(sysconfig.get_path("purelib"))
    if not (site / "music_assistant").is_dir():
        sys.exit(f"No music_assistant package in {site}")

    overlay = context / "overlay"
    if overlay.is_dir():
        shutil.copytree(overlay, site, dirs_exist_ok=True)
    deleted = context / "deleted.txt"
    for line in deleted.read_text().splitlines() if deleted.exists() else []:
        if line.startswith("music_assistant/"):
            (site / line).unlink(missing_ok=True)

    wheels = sorted(context.glob("music_assistant_frontend-*.whl"))
    if len(wheels) != 1:
        sys.exit(f"Expected one frontend wheel, found {[w.name for w in wheels]}")
    for old in [site / "music_assistant_frontend", *site.glob("music_assistant_frontend-*.dist-info")]:
        if old.is_dir():
            shutil.rmtree(old)
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel.extractall(site)

    # stale bytecode of a changed module would be ignored anyway (it checks the source mtime),
    # but compile now so the first start is as fast as the official image's
    compileall.compile_dir(site / "music_assistant", quiet=1)
    for path in (site / "music_assistant", site / "music_assistant_frontend"):
        _open_up(path)
    # this is no official release build anymore: let the server say so
    BUILD_INFO.unlink(missing_ok=True)


def _open_up(root: Path) -> None:
    """Give the replaced files the permissive modes the official image uses (any uid)."""
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o777)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o777)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
