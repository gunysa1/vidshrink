import os
import shutil
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone
from vidshrink.scanner import scan
from vidshrink.worker import AlreadyRunning, Worker, final_path, tmp_path


def test_paths():
    assert final_path("TV/S1/e.avi") == "TV/S1/e.mkv"
    assert final_path("a.mkv") == "a.mkv"
    assert tmp_path("TV/S1/e.avi") == "TV/S1/e.vidshrink-tmp.mkv"


@pytest.fixture
def env(conn, cfg, make_clip):
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    shutil.copy(make_clip("b", container="mp4"), root / "Movies" / "b.mp4")
    rc = Rclone(cfg.remote)

    def prober(path, size):
        return parse_ffprobe(run_ffprobe(str(root / path)), size)

    scan(conn, rc, cfg, prober, log=lambda s: None)
    budget = Budget(conn, cfg.daily_upload_limit)
    worker = Worker(conn, rc, cfg, budget, sleep=lambda s: None, log=lambda s: None)
    return root, worker


def test_run_replaces_files(conn, cfg, env):
    root, worker = env
    before = (root / "Movies" / "a.mkv").stat().st_size
    assert worker.run() == 2
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mkv"]
    assert (root / "Movies" / "a.mkv").stat().st_size < before * 0.8
    out = parse_ffprobe(run_ffprobe(str(root / "Movies" / "b.mkv")), 1)
    assert out.codec == "hevc"
    for path in ("Movies/a.mkv", "Movies/b.mp4"):
        row = db.get_by_path(conn, path)
        assert row["stage"] == "done" and row["out_size"] > 0 and row["encode_secs"] > 0
    assert list(cfg.workdir.glob("*.src")) == [] and list(cfg.workdir.glob("*.mkv")) == []
    assert db.learned_mean(conn, "ratio:h264", 0.55) != 0.55


def test_limit(conn, env):
    _, worker = env
    assert worker.run(limit=1) == 1
    assert len(db.rows_in(conn, "done")) == 1


def test_dry_run_keeps_original(conn, env):
    root, worker = env
    assert worker.run(dry_run=True) == 2
    names = sorted(p.name for p in (root / "Movies").iterdir())
    assert names == ["a.mkv", "a.vidshrink-tmp.mkv", "b.mp4", "b.vidshrink-tmp.mkv"]
    assert len(db.rows_in(conn, "dry_run")) == 2


def test_not_worth_it_leaves_original(conn, cfg, env):
    root, worker = env
    worker.cfg.min_saving = 0.999
    worker.run()
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]
    assert len(db.rows_in(conn, "not_worth_it")) == 2


def test_encode_failure_retries_then_fails(conn, env, monkeypatch):
    root, worker = env
    from vidshrink import worker as worker_mod
    from vidshrink.encode import EncodeError

    def boom(cmd, log_path):
        raise EncodeError("boom")

    monkeypatch.setattr(worker_mod, "run_encode", boom)
    assert worker.run() == 0
    rows = db.rows_in(conn, "failed")
    assert len(rows) == 2 and all(r["attempts"] == 2 and r["reason"] == "boom" for r in rows)


def test_budget_exhausted_buffers_then_waits(conn, env):
    root, worker = env
    worker.budget.add(worker.cfg.daily_upload_limit)  # today is used up
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt  # stop the loop at the first wait

    worker.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert len(db.rows_in(conn, "ready")) == 2  # both encoded, waiting for budget
    assert slept and slept[0] > 0


def test_rate_limit_blocks_uploads(conn, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError

    def limited(local, path):
        raise RcloneError(1, "userRateLimitExceeded")

    monkeypatch.setattr(worker.rclone, "upload", limited)
    worker.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert not worker.budget.allows(1)
    assert db.rows_in(conn, "ready")


def test_lock_prevents_second_worker(env):
    _, worker = env
    from vidshrink.worker import _lock
    with _lock(worker.cfg.workdir):
        with pytest.raises(AlreadyRunning):
            worker.run()


def test_target_name_taken(conn, cfg, env):
    root, worker = env
    (root / "Movies" / "b.mkv").write_bytes(b"someone else's file")
    worker.run()
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "failed" and "exists" in row["reason"]
    assert (root / "Movies" / "b.mp4").exists()


def test_swap_checks_target_again_and_stays_reconcilable(conn, cfg, env):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    row = db.get_by_path(conn, "Movies/b.mp4")
    worker.prepare(row)
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    # A foreign file races in under the final name after prepare's own check passed.
    (root / "Movies" / "b.mkv").write_bytes(b"someone else's file")
    assert worker.upload_and_swap(row, dry_run=False) is False
    assert (root / "Movies" / "b.mkv").read_bytes() == b"someone else's file"
    assert (root / "Movies" / "b.mp4").exists()
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "swapping" and "exists" in row["reason"]


def test_disk_too_small_fails_permanently(conn, env):
    root, worker = env
    worker.cfg.disk_reserve = 10**18
    slept = []
    worker.sleep = lambda s: slept.append(s)
    assert worker.run() == 0
    rows = db.rows_in(conn, "failed")
    assert len(rows) == 2 and all("disk" in r["reason"] for r in rows)
    assert not slept


@pytest.mark.parametrize("change", ["size", "modtime"])
def test_swap_refuses_when_original_changed(conn, env, change):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    row = db.get_by_path(conn, "Movies/b.mp4")
    worker.prepare(row)
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    orig = root / "Movies" / "b.mp4"
    if change == "size":
        orig.write_bytes(b"a different file")
    else:
        st = orig.stat()
        os.utime(orig, (st.st_atime, st.st_mtime - 3600))
    content = orig.read_bytes()
    assert worker.upload_and_swap(row, dry_run=False) is False
    assert orig.read_bytes() == content
    assert not (root / "Movies" / "b.mkv").exists()
    r = db.get_by_path(conn, "Movies/b.mp4")
    assert r["stage"] != "done" and "original changed or duplicated" in r["reason"]


def test_prepare_requeues_file_changed_since_scan(conn, cfg, env):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    with open(root / "Movies" / "a.mkv", "ab") as f:
        f.write(b"x" * 10)  # grew after the scan recorded its size
    worker.prepare(db.get_by_path(conn, "Movies/a.mkv"))
    r = db.get_by_path(conn, "Movies/a.mkv")
    assert (r["stage"], r["reason"]) == ("unprobed", "changed since scan")
    assert list(cfg.workdir.glob("*.src")) == [] and list(cfg.workdir.glob("*.mkv")) == []


def test_failed_swap_is_retried_during_the_run(conn, cfg, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError
    worker.cfg.retry_attempts = 1
    worker.cfg.swap_retry_secs = 0
    real_move = worker.rclone.move
    calls = []

    def flaky_move(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise RcloneError(1, "connection reset")
        real_move(src, dst)

    monkeypatch.setattr(worker.rclone, "move", flaky_move)
    assert worker.run() == 2
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mkv"]
    assert len(db.rows_in(conn, "done")) == 2 and not db.rows_in(conn, "swapping")
    assert len(calls) == 3


def test_swap_retry_waits_for_interval(conn, cfg, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError
    worker.cfg.retry_attempts = 1
    worker.clock = lambda: 1000.0  # time stands still: the 900 s interval never passes
    real_move = worker.rclone.move
    calls = []

    def flaky_move(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise RcloneError(1, "connection reset")
        real_move(src, dst)

    monkeypatch.setattr(worker.rclone, "move", flaky_move)
    assert worker.run() == 1
    assert len(db.rows_in(conn, "swapping")) == 1 and len(calls) == 2


@pytest.mark.parametrize("missing", ["rclone", "ffmpeg", "ffprobe"])
def test_missing_tool_stops_before_touching_rows(conn, env, monkeypatch, missing):
    _, worker = env
    from vidshrink import worker as worker_mod
    real_which = shutil.which
    monkeypatch.setattr(worker_mod.shutil, "which",
                        lambda name: None if name == missing else real_which(name))
    # An interrupted row would be failed by recover(); it must not even get that far.
    db.mark(conn, db.get_by_path(conn, "Movies/a.mkv")["id"], "encoding")
    before = [tuple(r) for r in conn.execute("SELECT * FROM videos ORDER BY id")]
    with pytest.raises(RuntimeError, match=missing):
        worker.run()
    assert [tuple(r) for r in conn.execute("SELECT * FROM videos ORDER BY id")] == before


def test_tool_vanishing_mid_run_crashes_without_burning_attempts(conn, env, monkeypatch):
    _, worker = env
    from vidshrink import worker as worker_mod

    def gone(cmd, log_path):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(worker_mod, "run_encode", gone)
    with pytest.raises(FileNotFoundError):
        worker.run()
    rows = db.rows_in(conn, "queued")
    assert len(rows) == 2 and all(r["attempts"] == 0 for r in rows)


def test_run_stops_after_consecutive_failures(conn, env, monkeypatch):
    _, worker = env
    from vidshrink import worker as worker_mod
    from vidshrink.encode import EncodeError
    worker.cfg.max_attempts = 100
    calls = []

    def boom(cmd, log_path):
        calls.append(1)
        raise EncodeError("boom")

    monkeypatch.setattr(worker_mod, "run_encode", boom)
    assert worker.run() == 0
    assert len(calls) == worker.cfg.max_consecutive_failures == 5
    rows = db.rows_in(conn, "queued")
    assert len(rows) == 2 and sum(r["attempts"] for r in rows) == 5


def test_run_stops_after_consecutive_upload_failures(conn, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError
    worker.cfg.max_attempts = 100
    worker.cfg.retry_attempts = 1
    calls = []

    def broken(local, path):
        calls.append(path)
        raise RcloneError(1, "connection reset")

    monkeypatch.setattr(worker.rclone, "upload", broken)
    assert worker.run() == 0
    assert len(calls) == 5
    # The encoded row keeps its output for later; the other was never started.
    assert len(db.rows_in(conn, "ready")) == 1 and len(db.rows_in(conn, "queued")) == 1
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]


def test_checksum_mismatch_after_upload(conn, cfg, env, monkeypatch):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    worker.prepare(db.get_by_path(conn, "Movies/b.mp4"))
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    out = cfg.workdir / f"{row['id']}.out.mkv"
    output = out.read_bytes()
    original = (root / "Movies" / "b.mp4").read_bytes()
    monkeypatch.setattr(worker.rclone, "md5", lambda path: "0" * 32)
    assert worker.upload_and_swap(row, dry_run=False) is False
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]
    assert (root / "Movies" / "b.mp4").read_bytes() == original
    r = db.get_by_path(conn, "Movies/b.mp4")
    assert (r["stage"], r["reason"], r["attempts"]) == ("ready", "checksum mismatch after upload", 1)
    assert out.read_bytes() == output
