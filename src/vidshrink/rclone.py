import json
import posixpath
import random
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from vidshrink import procflags
from vidshrink.procflags import TEXT_KWARGS

T = TypeVar("T")
TMP_SUFFIX = ".vidshrink-tmp.mkv"
# How long a cancelled transfer gets to exit after terminate() before kill().
CANCEL_GRACE_SECS = 5.0
# Drive's daily download quota (shared with anything else on the account, e.g. Plex).
DOWNLOAD_QUOTA_MARKER = "downloadQuotaExceeded"
# The remote's Drive login is dead (expired/revoked refresh token): no retry helps;
# it needs `rclone config reconnect <remote>:`.
AUTH_MARKERS = ("invalid_grant", "couldn't fetch token", "Token has been expired or revoked")
# "Received upload limit error" is what rclone 1.60 logs when --drive-stop-on-upload-limit
# turns Drive's daily-limit 403 into a fatal error.
RATE_LIMIT_MARKERS = ("userRateLimitExceeded", "uploadLimitExceeded", "User rate limit exceeded",
                      "Received upload limit error")


class TransferCancelled(Exception):
    """A download/upload was cancelled (Stop now / server cancel) before it finished."""


class RcloneError(Exception):
    def __init__(self, code: int, stderr: str):
        super().__init__(f"rclone exit {code}: {stderr.strip()[-500:]}")
        self.code = code
        self.stderr = stderr

    @property
    def rate_limited(self) -> bool:
        return any(m in self.stderr for m in RATE_LIMIT_MARKERS)

    @property
    def download_quota(self) -> bool:
        return DOWNLOAD_QUOTA_MARKER in self.stderr

    @property
    def auth_failed(self) -> bool:
        return any(m in self.stderr for m in AUTH_MARKERS)


class Rclone:
    def __init__(self, remote: str, extra_args: Sequence[str] = (), binary: str = "rclone"):
        self.remote = remote
        self.extra_args = list(extra_args)
        self.binary = binary

    def _p(self, path: str) -> str:
        return self.remote + path

    def _run(self, *args: str, cancel: threading.Event | None = None) -> str:
        cmd = [self.binary, *args, *self.extra_args]
        if cancel is None:
            res = subprocess.run(cmd, capture_output=True, **TEXT_KWARGS,
                                 **procflags.spawn_kwargs())
            if res.returncode != 0:
                raise RcloneError(res.returncode, res.stderr)
            return res.stdout
        # Cancellable path: poll so Stop now / a server cancel can interrupt a
        # transfer within about CANCEL_GRACE_SECS instead of waiting it out.
        # Must poll via communicate(), not wait(): rclone can write more to
        # stdout/stderr than the OS pipe buffer holds (its own progress/verbose
        # output on stderr, easily), and wait() never drains those pipes -- once
        # the child blocks on a full pipe, wait() blocks forever with it. Calling
        # communicate(timeout=...) repeatedly after a TimeoutExpired is the
        # documented-safe way to poll while still draining both pipes.
        # Own process group/session: a Ctrl-C in the PC worker's console must not
        # kill rclone behind our back; we stop it ourselves and release the job.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **TEXT_KWARGS, **procflags.spawn_kwargs(new_group=True))
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set():
                    proc.terminate()  # Windows-safe: no signals, terminate()/kill() only
                    try:
                        out, err = proc.communicate(timeout=CANCEL_GRACE_SECS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        out, err = proc.communicate()
                    raise TransferCancelled("transfer cancelled") from None
        if proc.returncode != 0:
            raise RcloneError(proc.returncode, err)
        return out

    def list_videos(self, extensions: Sequence[str]) -> list[dict]:
        pattern = "*.{" + ",".join(extensions) + "}"
        # --fast-list: one recursive query instead of a request per folder; on Drive
        # this turned a ~20 min listing into ~75 s (108k files, 2026-10-18).
        out = self._run("lsjson", "-R", "--fast-list", "--files-only", "--no-mimetype",
                        "--ignore-case", "--include", pattern, self.remote)
        # Our own upload temp files are not videos to shrink.
        return [e for e in json.loads(out) if not e["Path"].endswith(TMP_SUFFIX)]

    def download(self, path: str, local: Path, cancel: threading.Event | None = None) -> None:
        self._run("copyto", self._p(path), str(local), cancel=cancel)

    def upload(self, local: Path, path: str, cancel: threading.Event | None = None) -> None:
        self._run("copyto", str(local), self._p(path), cancel=cancel)

    def delete(self, path: str) -> None:
        self._run("deletefile", self._p(path))

    def move(self, src: str, dst: str) -> None:
        self._run("moveto", self._p(src), self._p(dst))

    def entries(self, path: str) -> list[dict]:
        """All files in path's folder named like path (Drive allows duplicate names)."""
        parent, name = posixpath.split(path)
        try:
            out = self._run("lsjson", "--files-only", "--no-mimetype", self._p(parent))
        except RcloneError as e:
            if e.code == 3 or "directory not found" in e.stderr:
                return []
            raise
        return [e for e in json.loads(out) if e["Name"] == name]

    def exists(self, path: str) -> bool:
        return bool(self.entries(path))

    def md5(self, path: str) -> str | None:
        out = self._run("md5sum", self._p(path)).split()
        return out[0] if out else None


def with_retries(fn: Callable[[], T], attempts: int, base_delay: float,
                 sleep: Callable[[float], None] = time.sleep) -> T:
    for i in range(attempts):
        try:
            return fn()
        except RcloneError as e:
            if e.rate_limited or e.download_quota or e.auth_failed or i == attempts - 1:
                raise
            sleep(base_delay * 2 ** i + random.uniform(0, base_delay))
    raise AssertionError("unreachable")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_http(rclone: Rclone, port: int, rc_port: int | None = None,
               serve_args: Sequence[str] = ()):
    """Read-only HTTP view of the remote. With rc_port, rclone's stats (bytes served,
    last error) can be read with served_stats(rc_port)."""
    rc = ["--rc", "--rc-addr", f"127.0.0.1:{rc_port}", "--rc-no-auth"] if rc_port else []
    proc = subprocess.Popen(
        [rclone.binary, "serve", "http", rclone.remote, "--addr", f"127.0.0.1:{port}",
         "--read-only", "--tpslimit", "10", *serve_args, *rc, *rclone.extra_args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **procflags.spawn_kwargs(),
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RcloneError(proc.returncode or 1, "rclone serve http did not start")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def served_stats(rc_port: int) -> dict:
    """rclone core/stats of a serve_http started with rc_port: 'bytes' read from the
    remote so far and 'lastError'."""
    req = urllib.request.Request(f"http://127.0.0.1:{rc_port}/core/stats", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())
