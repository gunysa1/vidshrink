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
