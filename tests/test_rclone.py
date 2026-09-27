import hashlib
import urllib.parse
import urllib.request

import pytest

from vidshrink.rclone import Rclone, RcloneError, free_port, serve_http, with_retries


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
