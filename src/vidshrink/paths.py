"""Drive path naming and file hashing shared by the server and the PC (no fcntl)."""
import hashlib
import posixpath
from pathlib import Path

from vidshrink.rclone import TMP_SUFFIX


def final_path(path: str) -> str:
    return posixpath.splitext(path)[0] + ".mkv"


def tmp_path(path: str) -> str:
    return posixpath.splitext(path)[0] + TMP_SUFFIX


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()
