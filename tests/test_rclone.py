import hashlib
import threading
import time
import urllib.parse
import urllib.request

import pytest

from vidshrink.config import Config
from vidshrink.rclone import (Rclone, RcloneError, TransferCancelled, free_port, serve_http,
                              with_retries)


@pytest.fixture
def drive(tmp_path):
    root = tmp_path / "drive"
    (root / "Movies" / "A [1080p]").mkdir(parents=True)
    (root / "Movies" / "A [1080p]" / "a.mkv").write_bytes(b"x" * 1000)
    (root / "notes.txt").write_text("hi")
    return root


@pytest.fixture
def rc(drive):
    return Rclone(str(drive) + "/")


def test_list_videos(rc):
    entries = rc.list_videos(["mkv", "mp4"])
    assert [(e["Path"], e["Size"]) for e in entries] == [("Movies/A [1080p]/a.mkv", 1000)]


def test_exists_and_md5(rc):
    assert rc.exists("Movies/A [1080p]/a.mkv")
    assert not rc.exists("Movies/A [1080p]/nope.mkv")
    assert not rc.exists("NoDir/a.mkv")
    assert rc.md5("Movies/A [1080p]/a.mkv") == hashlib.md5(b"x" * 1000).hexdigest()


def test_entries_returns_all_matches(rc):
    found = rc.entries("Movies/A [1080p]/a.mkv")
    assert [(e["Name"], e["Size"]) for e in found] == [("a.mkv", 1000)]
    assert found[0]["ModTime"]
    assert rc.entries("Movies/A [1080p]/nope.mkv") == []
    assert rc.entries("NoDir/a.mkv") == []


def test_list_videos_ignores_vidshrink_tmp(rc, drive):
    (drive / "Movies" / "A [1080p]" / "a.vidshrink-tmp.mkv").write_bytes(b"y")
    assert [e["Path"] for e in rc.list_videos(["mkv"])] == ["Movies/A [1080p]/a.mkv"]


def test_download_upload_move_delete(rc, tmp_path):
    local = tmp_path / "local.bin"
    rc.download("Movies/A [1080p]/a.mkv", local)
    assert local.read_bytes() == b"x" * 1000
    rc.upload(local, "Movies/A [1080p]/b.tmp.mkv")
    rc.move("Movies/A [1080p]/b.tmp.mkv", "Movies/A [1080p]/b.mkv")
    assert rc.exists("Movies/A [1080p]/b.mkv") and not rc.exists("Movies/A [1080p]/b.tmp.mkv")
    rc.delete("Movies/A [1080p]/b.mkv")
    assert not rc.exists("Movies/A [1080p]/b.mkv")


def test_errors_raise(rc, tmp_path):
    with pytest.raises(RcloneError):
        rc.download("missing.mkv", tmp_path / "x")


def test_rate_limited_detection():
    assert RcloneError(1, "googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded").rate_limited
    assert not RcloneError(1, "connection reset").rate_limited


def test_with_retries_retries_then_succeeds():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RcloneError(1, "connection reset")
        return "ok"

    assert with_retries(fn, attempts=5, base_delay=1.0, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_with_retries_gives_up():
    def fn():
        raise RcloneError(1, "boom")

    with pytest.raises(RcloneError):
        with_retries(fn, attempts=3, base_delay=0, sleep=lambda s: None)


def test_with_retries_does_not_retry_rate_limit():
    calls = []

    def fn():
        calls.append(1)
        raise RcloneError(1, "userRateLimitExceeded")

    with pytest.raises(RcloneError):
        with_retries(fn, attempts=5, base_delay=0, sleep=lambda s: None)
    assert len(calls) == 1


def test_serve_http(rc):
    with serve_http(rc, free_port()) as base:
        url = base + urllib.parse.quote("Movies/A [1080p]/a.mkv")
        assert urllib.request.urlopen(url).read() == b"x" * 1000


def test_stop_on_upload_limit_error_is_rate_limited():
    # What --drive-stop-on-upload-limit logs (rclone 1.60 backend/drive) before giving up.
    stderr = ("ERROR : gdrive root: Received upload limit error: googleapi: Error 403: "
              "User rate limit exceeded., userRateLimitExceeded\n"
              "ERROR : Fatal error received - not attempting retries\n")
    assert RcloneError(1, stderr).rate_limited
    assert RcloneError(1, "ERROR : x: Received upload limit error: googleapi: Error 403").rate_limited
    assert not RcloneError(1, "ERROR : Fatal error received - not attempting retries").rate_limited


def test_default_rclone_args_are_pinned_and_accepted(tmp_path):
    args = Config().rclone_args
    assert args == ("--drive-chunk-size", "64M", "--drive-use-trash=true",
                    "--drive-stop-on-upload-limit", "--retries", "1")
    (tmp_path / "v.mkv").write_bytes(b"x")
    rc = Rclone(str(tmp_path) + "/", args)  # the installed rclone must accept every flag
    assert [e["Path"] for e in rc.list_videos(["mkv"])] == ["v.mkv"]


def test_download_quota_detection():
    err = RcloneError(1, "googleapi: Error 403: The download quota for this file has been "
                         "exceeded., downloadQuotaExceeded")
    assert err.download_quota and not err.rate_limited
    assert not RcloneError(1, "connection reset").download_quota


def test_serve_http_reports_served_bytes(rc):
    from vidshrink.rclone import served_stats
    rc_port = free_port()
    with serve_http(rc, free_port(), rc_port=rc_port) as base:
        req = urllib.request.Request(base + urllib.parse.quote("Movies/A [1080p]/a.mkv"),
                                     headers={"Range": "bytes=0-99"})
        assert len(urllib.request.urlopen(req).read()) == 100
        stats = served_stats(rc_port)
    assert stats["bytes"] == 100
    assert "downloadQuotaExceeded" not in stats.get("lastError", "")


def test_cancel_interrupts_transfer(rc, tmp_path):
    local = tmp_path / "big.bin"
    local.write_bytes(b"x" * (20 * 1024 * 1024))
    slow = Rclone(rc.remote, ("--bwlimit", "1M"))
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(TransferCancelled):
        slow.upload(local, "slow.mkv", cancel=cancel)
    assert time.monotonic() - start < 10


def test_no_cancel_arg_behaves_as_before(rc, tmp_path):
    # cancel=None (the default) must not change behaviour at all.
    local = tmp_path / "x.bin"
    local.write_bytes(b"y" * 100)
    rc.upload(local, "y.mkv", cancel=None)
    assert rc.exists("y.mkv")


def test_cancellable_run_does_not_deadlock_on_large_stderr(tmp_path):
    # Regression test: the cancellable path used to poll with proc.wait(), which
    # never drains stdout/stderr. A child that writes more than the OS pipe
    # buffer (commonly 64 KiB) to a PIPE it never reads back blocks on write()
    # once the buffer fills, and wait() then blocks forever waiting for an exit
    # that can't happen -- a deadlock. Guarded by running it on a thread and
    # joining with a timeout, so a regression fails the test instead of hanging
    # the whole suite.
    script = tmp_path / "noisy.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('x' * 200_000)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    rc = Rclone("remote:", binary=str(script))
    result = {}

    def go():
        try:
            rc._run(cancel=threading.Event())
        except RcloneError as e:
            result["error"] = e
        except Exception as e:  # pragma: no cover - would indicate a different bug
            result["unexpected"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "cancellable _run hung (pipe deadlock?)"
    assert isinstance(result.get("error"), RcloneError)
    assert result["error"].code == 1


def test_with_retries_does_not_retry_download_quota():
    calls = []

    def fn():
        calls.append(1)
        raise RcloneError(1, "Error 403: downloadQuotaExceeded")

    with pytest.raises(RcloneError):
        with_retries(fn, attempts=5, base_delay=0, sleep=lambda s: None)
    assert len(calls) == 1
