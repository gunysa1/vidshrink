"""Drive path naming and file hashing shared by the server and the PC (no fcntl)."""
import hashlib
import posixpath
import re
from pathlib import Path

from vidshrink.rclone import TMP_SUFFIX

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


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


def is_valid_md5(value) -> bool:
    """True iff value looks like a real md5 hex digest (32 lowercase hex chars)."""
    return isinstance(value, str) and bool(_MD5_RE.fullmatch(value))
