import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, TransferCancelled
from vidshrink.scanner import scan
from vidshrink.worker import AlreadyRunning, Worker, final_path, tmp_path


def prepare(worker, row):
    """Download and encode one row on this thread, the way the loop does in stages."""
    if not worker.start_download(row):
        return
    try:
        worker._download(row)
    except Exception as e:
        worker.finish_download(row, error=e)
        return
    if worker.finish_download(row):
        worker.encode_and_verify(db.get(worker.conn, row["id"]))


def upload_and_swap(worker, row, dry_run=False):
    """Upload one ready row and swap it, waiting for the background transfer."""
    transfer = worker.start_upload(row)
    transfer.wait(60)
    error = None
    try:
        transfer.result()
    except Exception as e:
        error = e
    return worker.finish_upload(row, error, dry_run)


def wait_until(cond, tick, timeout=30.0):
    """Call the encode tick until cond() holds; fail the test if it never does."""
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition never reached during the encode"
        tick()
        time.sleep(0.05)


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

    def boom(cmd, log_path, **kwargs):
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

    def limited(local, path, **kw):
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
    prepare(worker, row)
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    # A foreign file races in under the final name after prepare's own check passed.
    (root / "Movies" / "b.mkv").write_bytes(b"someone else's file")
    assert upload_and_swap(worker, row) is False
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
    prepare(worker, row)
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    orig = root / "Movies" / "b.mp4"
    if change == "size":
        orig.write_bytes(b"a different file")
    else:
        st = orig.stat()
        os.utime(orig, (st.st_atime, st.st_mtime - 3600))
    content = orig.read_bytes()
    assert upload_and_swap(worker, row) is False
    assert orig.read_bytes() == content
    assert not (root / "Movies" / "b.mkv").exists()
    r = db.get_by_path(conn, "Movies/b.mp4")
    assert r["stage"] != "done" and "original changed or duplicated" in r["reason"]


def test_prepare_requeues_file_changed_since_scan(conn, cfg, env):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    with open(root / "Movies" / "a.mkv", "ab") as f:
        f.write(b"x" * 10)  # grew after the scan recorded its size
    prepare(worker, db.get_by_path(conn, "Movies/a.mkv"))
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

    def gone(cmd, log_path, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(worker_mod, "run_encode", gone)
    with pytest.raises(FileNotFoundError):
        worker.run()
    rows = db.rows_in(conn, "downloaded", "downloading", "queued")
    assert len(rows) == 2 and all(r["attempts"] == 0 for r in rows)
    # The file being encoded keeps its complete download for the next start.
    encoded = db.rows_in(conn, "downloaded")
    assert len(encoded) == 1 and worker._src(encoded[0]["id"]).exists()
    worker.recover()
    stages = sorted(r["stage"] for r in db.rows_in(conn, "downloaded", "queued"))
    assert stages == ["downloaded", "queued"]


def test_run_stops_after_consecutive_failures(conn, env, monkeypatch):
    _, worker = env
    from vidshrink import worker as worker_mod
    from vidshrink.encode import EncodeError
    worker.cfg.max_attempts = 100
    calls = []

    def boom(cmd, log_path, **kwargs):
        calls.append(1)
        raise EncodeError("boom")

    monkeypatch.setattr(worker_mod, "run_encode", boom)
    assert worker.run() == 0
    assert len(calls) == worker.cfg.max_consecutive_failures == 5
    # The next file may already be downloaded ahead; it is kept, not thrown away.
    rows = db.rows_in(conn, "queued", "downloaded")
    assert len(rows) == 2 and sum(r["attempts"] for r in rows) == 5


def test_run_stops_after_consecutive_upload_failures(conn, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError
    worker.cfg.max_attempts = 100
    worker.cfg.retry_attempts = 1
    calls = []

    def broken(local, path, **kw):
        calls.append(path)
        raise RcloneError(1, "connection reset")

    monkeypatch.setattr(worker.rclone, "upload", broken)
    assert worker.run() == 0
    # The second encode can finish (a success) between upload failures, so the
    # breaker trips after 5 failures in a row since then, not 5 in total.
    assert worker.failures == worker.cfg.max_consecutive_failures == 5
    assert 5 <= len(calls) <= 7
    # The encoded row keeps its output for later, and the other (fetched or encoded
    # while the uploads failed) keeps its download or its output.
    rows = db.rows_in(conn, "ready", "downloaded")
    assert len(rows) == 2 and db.rows_in(conn, "ready")
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]


def test_checksum_mismatch_after_upload(conn, cfg, env, monkeypatch):
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    prepare(worker, db.get_by_path(conn, "Movies/b.mp4"))
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "ready"
    out = cfg.workdir / f"{row['id']}.out.mkv"
    output = out.read_bytes()
    original = (root / "Movies" / "b.mp4").read_bytes()
    monkeypatch.setattr(worker.rclone, "md5", lambda path: "0" * 32)
    assert upload_and_swap(worker, row) is False
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]
    assert (root / "Movies" / "b.mp4").read_bytes() == original
    r = db.get_by_path(conn, "Movies/b.mp4")
    assert (r["stage"], r["reason"], r["attempts"]) == ("ready", "checksum mismatch after upload", 1)
    assert out.read_bytes() == output


def test_download_budget_counts_downloads(conn, env):
    _, worker = env
    worker.run(limit=1)
    assert len(db.rows_in(conn, "done")) == 1
    downloaded = [r for r in db.rows_in(conn, "done", "ready", "downloaded")]
    assert worker.download_budget.used() == sum(r["size"] for r in downloaded)


def test_download_budget_exhausted_waits_without_downloading(conn, env):
    _, worker = env
    worker.download_budget.add(worker.cfg.daily_download_limit)
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    worker.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert slept and slept[0] > 0
    assert len(db.rows_in(conn, "queued")) == 2  # nothing downloaded


def test_download_quota_error_blocks_downloads_without_burning_attempts(conn, env, monkeypatch):
    from vidshrink.rclone import RcloneError
    _, worker = env

    def quota(path, local, **kw):
        raise RcloneError(1, "googleapi: Error 403: The download quota for this file has "
                             "been exceeded., downloadQuotaExceeded")

    monkeypatch.setattr(worker.rclone, "download", quota)
    worker.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    rows = db.rows_in(conn, "queued")
    assert len(rows) == 2 and all(r["attempts"] == 0 for r in rows)
    assert not worker.download_budget.allows(1)


def test_missing_non_tool_file_fails_the_file_instead_of_crashing(conn, cfg, env, monkeypatch):
    # e.g. ffmpeg exits 0 but writes no output: this file's problem, not a missing tool,
    # so it must not crash the service (which would re-download it on every restart).
    _, worker = env
    from vidshrink import worker as worker_mod

    def no_output(cmd, log_path, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(cfg.workdir / "1.out.mkv"))

    monkeypatch.setattr(worker_mod, "run_encode", no_output)
    assert worker.run() == 0  # returns normally
    rows = db.rows_in(conn, "failed")
    assert len(rows) == 2 and all(r["attempts"] == 2 for r in rows)


def test_finish_download_routes_transfer_errors(conn, cfg, env):
    from vidshrink.rclone import RcloneError
    _, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    a, b = db.get_by_path(conn, "Movies/a.mkv"), db.get_by_path(conn, "Movies/b.mp4")

    def started(row):
        assert worker.start_download(row)
        worker._src(row["id"]).write_bytes(b"partial")
        return db.get(conn, row["id"])

    quota = RcloneError(1, "googleapi: Error 403: downloadQuotaExceeded")
    assert worker.finish_download(started(a), error=quota) is False
    r = db.get(conn, a["id"])
    assert (r["stage"], r["attempts"]) == ("queued", 0)
    assert not worker.download_budget.allows(1) and not worker._src(a["id"]).exists()

    assert worker.finish_download(started(b), error=RcloneError(1, "connection reset")) is False
    r = db.get(conn, b["id"])
    assert (r["stage"], r["attempts"]) == ("queued", 1) and not worker._src(b["id"]).exists()

    gone = FileNotFoundError(2, "No such file or directory", "rclone")
    with pytest.raises(FileNotFoundError):
        worker.finish_download(started(a), error=gone)
    r = db.get(conn, a["id"])
    assert (r["stage"], r["attempts"]) == ("queued", 0) and not worker._src(a["id"]).exists()


def test_server_downloads_next_during_encode(conn, env, monkeypatch):
    _, worker = env
    from vidshrink import worker as worker_mod
    real_run_encode = worker_mod.run_encode
    seen = []

    def encode(cmd, log_path, tick=None, tick_secs=60.0):
        if not seen:
            seen.append(1)
            # The other file is fetched in the background while this one encodes.
            wait_until(lambda: len(db.rows_in(conn, "downloaded")) == 1, tick)
        return real_run_encode(cmd, log_path)

    monkeypatch.setattr(worker_mod, "run_encode", encode)
    assert worker.run() == 2
    assert seen and len(db.rows_in(conn, "done")) == 2


def test_server_uploads_during_encode(conn, env, monkeypatch):
    root, worker = env
    from vidshrink import worker as worker_mod
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    first = db.get_by_path(conn, "Movies/b.mp4")
    prepare(worker, first)
    assert db.get(conn, first["id"])["stage"] == "ready"
    real_run_encode = worker_mod.run_encode
    real_upload = worker.rclone.upload
    encoding = threading.Event()
    states = []

    def upload(local, path, **kw):
        # Held until the next encode is under way: a serial worker never gets there.
        assert encoding.wait(30), "upload was not running alongside the encode"
        real_upload(local, path, **kw)

    def encode(cmd, log_path, tick=None, tick_secs=60.0):
        encoding.set()
        wait_until(lambda: db.get(conn, first["id"])["stage"] == "done", tick)
        states.append(db.get(conn, first["id"])["stage"])
        return real_run_encode(cmd, log_path)

    monkeypatch.setattr(worker.rclone, "upload", upload)
    monkeypatch.setattr(worker_mod, "run_encode", encode)
    assert worker.run() == 2
    assert states == ["done"]
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mkv"]


def test_server_download_quota_in_background_blocks_and_requeues(conn, env, monkeypatch):
    from vidshrink import worker as worker_mod
    from vidshrink.rclone import RcloneError
    _, worker = env
    real_run_encode = worker_mod.run_encode
    real_download = worker.rclone.download
    downloads = []

    def download(path, local, **kw):
        downloads.append(path)
        if len(downloads) == 2:
            raise RcloneError(1, "googleapi: Error 403: The download quota for this file "
                                 "has been exceeded., downloadQuotaExceeded")
        real_download(path, local, **kw)

    def encode(cmd, log_path, tick=None, tick_secs=60.0):
        wait_until(lambda: not worker.download_budget.allows(1), tick)
        return real_run_encode(cmd, log_path)

    monkeypatch.setattr(worker.rclone, "download", download)
    monkeypatch.setattr(worker_mod, "run_encode", encode)
    worker.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    second = db.get_by_path(conn, downloads[1])
    assert (second["stage"], second["attempts"]) == ("queued", 0)
    assert not worker._src(second["id"]).exists()
    assert not worker.download_budget.allows(1)
    assert db.get_by_path(conn, downloads[0])["stage"] == "done"


def test_dry_run_still_never_swaps(conn, env, monkeypatch):
    root, worker = env
    from vidshrink import worker as worker_mod
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    first = db.get_by_path(conn, "Movies/b.mp4")
    prepare(worker, first)
    real_run_encode = worker_mod.run_encode
    real_upload = worker.rclone.upload
    encoding = threading.Event()

    def upload(local, path, **kw):
        assert encoding.wait(30), "upload was not running alongside the encode"
        real_upload(local, path, **kw)

    def never(*a, **k):
        raise AssertionError("a dry run must not swap, move or delete")

    def encode(cmd, log_path, tick=None, tick_secs=60.0):
        encoding.set()
        # The upload finishes during this encode, and still only ends in dry_run.
        wait_until(lambda: db.get(conn, first["id"])["stage"] == "dry_run", tick)
        return real_run_encode(cmd, log_path)

    monkeypatch.setattr(worker.rclone, "upload", upload)
    monkeypatch.setattr(worker, "_swap", never)
    monkeypatch.setattr(worker.rclone, "move", never)
    monkeypatch.setattr(worker.rclone, "delete", never)
    monkeypatch.setattr(worker_mod, "run_encode", encode)
    assert worker.run(dry_run=True) == 2
    names = sorted(p.name for p in (root / "Movies").iterdir())
    assert names == ["a.mkv", "a.vidshrink-tmp.mkv", "b.mp4", "b.vidshrink-tmp.mkv"]
    assert len(db.rows_in(conn, "dry_run")) == 2


def test_interrupted_run_leaves_unfinished_transfers_for_recover(conn, cfg, env, make_clip,
                                                                 monkeypatch):
    root, worker = env
    from vidshrink import worker as worker_mod
    shutil.copy(make_clip("c"), root / "Movies" / "c.mkv")
    scan(conn, worker.rclone, cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    ready = db.get_by_path(conn, "Movies/b.mp4")
    prepare(worker, ready)                                   # -> ready, will upload
    waiting = db.get_by_path(conn, "Movies/a.mkv")
    assert worker.start_download(waiting)
    worker._download(waiting)
    assert worker.finish_download(waiting)                   # -> downloaded, will encode
    fetched = db.get_by_path(conn, "Movies/c.mkv")           # will be fetched ahead
    cancelled = []

    def hang(*a, cancel=None, **k):
        assert cancel is not None and cancel.wait(30)
        cancelled.append(a[0])
        raise TransferCancelled("transfer cancelled")

    def encode(cmd, log_path, tick=None, tick_secs=60.0):
        wait_until(lambda: db.get(conn, ready["id"])["stage"] == "uploading"
                   and db.get(conn, fetched["id"])["stage"] == "downloading", tick)
        raise KeyboardInterrupt  # the service is stopped mid-encode

    monkeypatch.setattr(worker.rclone, "upload", hang)
    monkeypatch.setattr(worker.rclone, "download", hang)
    monkeypatch.setattr(worker_mod, "run_encode", encode)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    # Both background transfers were stopped and nothing recorded them as finished.
    assert len(cancelled) == 2
    assert db.get(conn, ready["id"])["stage"] == "uploading"
    assert db.get(conn, fetched["id"])["stage"] == "downloading"
    assert db.get(conn, waiting["id"])["stage"] == "encoding"
    assert worker.download_budget.used() == ready["size"] + waiting["size"]
    worker.recover()
    assert db.get(conn, ready["id"])["stage"] == "ready"
    assert db.get(conn, waiting["id"])["stage"] == "downloaded"
    r = db.get(conn, fetched["id"])
    assert (r["stage"], r["attempts"]) == ("queued", 0)
