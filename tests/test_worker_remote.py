import hashlib
import shutil
import time
from pathlib import Path

import pytest

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.jobs import PC_SEEN_KEY
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError
from vidshrink.scanner import scan
from vidshrink.worker import SwapError, Worker

PC_OUT = b"encoded on the PC"
PC_MD5 = hashlib.md5(PC_OUT).hexdigest()


@pytest.fixture
def env(conn, cfg, make_clip):
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    rc = Rclone(cfg.remote)
    scan(conn, rc, cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)
    worker = Worker(conn, rc, cfg, Budget(conn, cfg.daily_upload_limit),
                    sleep=lambda s: None, log=lambda s: None)
    return root, worker


def as_remote_ready(conn, root, md5=PC_MD5):
    row = db.get_by_path(conn, "Movies/a.mkv")
    (root / "Movies" / "a.vidshrink-tmp.mkv").write_bytes(PC_OUT)
    db.mark(conn, row["id"], "remote_ready", out_size=len(PC_OUT), out_md5=md5,
            encode_secs=7.0, final_path="Movies/a.mkv", encoder="nvenc")
    return row["id"]


def test_server_swaps_verified_pc_result(conn, env):
    root, worker = env
    vid = as_remote_ready(conn, root)
    assert worker.run(limit=1) == 1
    assert (root / "Movies" / "a.mkv").read_bytes() == PC_OUT
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert db.get(conn, vid)["stage"] == "done"
    assert scorer.speed_for(conn, "sd", "nvenc") != scorer.DEFAULT_SPEED_NVENC["sd"]
    assert scorer.speed_for(conn, "sd") == scorer.DEFAULT_SPEED["sd"]


def test_server_rejects_mismatched_pc_result(conn, env):
    root, worker = env
    vid = as_remote_ready(conn, root, md5="0" * 32)
    original = (root / "Movies" / "a.mkv").read_bytes()
    worker._finish_remote(db.get(conn, vid))
    assert (root / "Movies" / "a.mkv").read_bytes() == original
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 1)
    # A PC-side upload mismatch is the PC's failure, not the server's; it must not
    # trip the server's own consecutive-failure breaker.
    assert worker.failures == 0


@pytest.mark.parametrize("bad_md5,bad_size", [("not-32-hex-chars", None), (None, 0)])
def test_finish_remote_rejects_invalid_report_fields(conn, env, bad_md5, bad_size):
    """Minor: a missing/non-32-hex out_md5 or non-positive out_size is treated as a
    mismatch even if it happens to match what's on Drive by coincidence."""
    root, worker = env
    md5 = bad_md5 if bad_md5 is not None else PC_MD5
    vid = as_remote_ready(conn, root, md5=md5)
    if bad_size is not None:
        db.mark(conn, vid, "remote_ready", out_size=bad_size)
    worker._finish_remote(db.get(conn, vid))
    assert (root / "Movies" / "a.mkv").read_bytes() != PC_OUT
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert db.get(conn, vid)["stage"] == "queued"


def test_swap_refuses_when_tmp_no_longer_matches_out_md5(conn, env):
    """Requirement 1(i): defense in depth in _swap itself -- even if something got
    past _finish_remote's own check, _swap must re-verify right before deleting the
    original."""
    root, worker = env
    row = db.get_by_path(conn, "Movies/a.mkv")
    original = (root / "Movies" / "a.mkv").read_bytes()
    tampered = b"tampered after verification"
    (root / "Movies" / "a.vidshrink-tmp.mkv").write_bytes(tampered)
    db.mark(conn, row["id"], "swapping", out_size=len(tampered),
            out_md5=hashlib.md5(b"something else entirely").hexdigest(),
            final_path="Movies/a.mkv", encode_secs=1.0)
    with pytest.raises(SwapError):
        worker._swap(db.get(conn, row["id"]))
    assert (root / "Movies" / "a.mkv").read_bytes() == original
    assert (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert db.get(conn, row["id"])["stage"] != "done"


def test_prepare_leaves_row_queued_if_its_stem_becomes_busy(conn, env, make_clip):
    """Requirement 1(ii): a row whose final_path becomes busy (another row with the
    same stem gets leased) after _next_job() returned it must be left queued, with
    nothing downloaded, when prepare() re-checks atomically. Uses two rows whose
    shared final_path ("Movies/x.mkv") doesn't already exist on Drive, so this
    exercises the busy-stem race rather than the unrelated "target already exists"
    check."""
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    # Extensions on the two source files, not their actual container -- ffprobe reads
    # the real content, so this is enough to create a same-stem, non-mkv collision.
    shutil.copy(make_clip("x1", container="mp4"), root / "Movies" / "x.avi")
    shutil.copy(make_clip("x2", container="mp4"), root / "Movies" / "x.mov")
    rc = Rclone(worker.cfg.remote)
    scan(conn, rc, worker.cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)

    avi_row = db.get_by_path(conn, "Movies/x.avi")
    mov_row_snapshot = db.get_by_path(conn, "Movies/x.mov")  # as _next_job() would see it
    assert mov_row_snapshot["stage"] == "queued"
    assert not worker.rclone.exists("Movies/x.mkv")

    # Another row claims the shared stem (Movies/x.mkv) after _next_job() returned
    # the mov row but before prepare() got to switch it to downloading.
    db.mark(conn, avi_row["id"], "leased", worker="pc", lease_until=time.time() + 600)

    worker.prepare(mov_row_snapshot)

    after = db.get(conn, mov_row_snapshot["id"])
    assert after["stage"] == "queued"
    assert not worker._src(mov_row_snapshot["id"]).exists()


def test_run_does_not_hot_loop_on_remote_check_errors(conn, env, make_clip):
    """Requirement 2: a RcloneError while checking a remote_ready row on Drive must
    not be retried every loop iteration; the server should get on with its own
    queued work instead."""
    root, worker = env
    shutil.copy(make_clip("b"), root / "Movies" / "b.mkv")
    rc = Rclone(worker.cfg.remote)
    scan(conn, rc, worker.cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)
    as_remote_ready(conn, root)

    tmp = "Movies/a.vidshrink-tmp.mkv"
    real_entries = worker.rclone.entries
    calls = []

    def flaky_entries(path):
        calls.append(path)
        if path == tmp:
            raise RcloneError(1, "simulated check failure")
        return real_entries(path)

    worker.rclone.entries = flaky_entries
    done = worker.run(limit=1)
    assert done == 1
    assert db.get_by_path(conn, "Movies/b.mkv")["stage"] == "done"
    assert calls.count(tmp) <= 2


def test_handover_leaves_long_jobs_to_the_pc(conn, env):
    _, worker = env
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    db.learn(conn, "speed:sd", 1e7)  # make the clip "take" far more than 6 h on the server
    db.kv_set(conn, PC_SEEN_KEY, str(time.time()))
    assert worker._next_job() is None
    db.kv_set(conn, PC_SEEN_KEY, str(time.time() - 4 * 86400))  # PC absent > 3 days
    assert worker._next_job()["id"] == vid


def test_run_waits_instead_of_exiting_while_pc_holds_work(conn, env):
    _, worker = env
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    db.mark(conn, vid, "leased", worker="pc", encoder="nvenc", lease_until=time.time() + 600)
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    worker.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert slept == [worker.cfg.idle_poll_secs]


def test_run_sweeps_expired_leases(conn, env):
    root, worker = env
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    db.mark(conn, vid, "leased", worker="pc", encoder="nvenc", lease_until=time.time() - 1)
    (root / "Movies" / "a.vidshrink-tmp.mkv").write_bytes(b"partial")
    worker.run(limit=1)  # sweep requeues it, then the server encodes it itself
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert db.get(conn, vid)["stage"] == "done"
    assert db.get(conn, vid)["encoder"] == "x265"


def test_prepare_does_not_overwrite_a_row_leased_in_the_meantime(conn, env):
    """Requirement A: no double assignment. A row that was queued when _next_job()
    picked it may get leased to the PC before prepare() marks it downloading;
    prepare() must detect this via mark_if and leave the row (and files) alone."""
    root, worker = env
    worker.cfg.workdir.mkdir(parents=True, exist_ok=True)
    row = db.get_by_path(conn, "Movies/a.mkv")  # snapshot while still queued
    assert row["stage"] == "queued"
    db.mark(conn, row["id"], "leased", worker="pc", lease_until=time.time() + 600)

    worker.prepare(row)

    after = db.get(conn, row["id"])
    assert after["stage"] == "leased"
    assert after["worker"] == "pc"
    assert not worker._src(row["id"]).exists()


def test_next_job_skips_queued_row_sharing_a_stem_with_a_busy_row(conn, env, make_clip):
    """Requirement B: same-stem collisions. Movies/a.avi and Movies/a.mp4 would both
    want Movies/a.mkv as their final_path; _next_job() must not hand out the mp4
    while the avi occupies that stem."""
    root, worker = env
    shutil.copy(make_clip("a2", container="mp4"), root / "Movies" / "a.mp4")
    rc = Rclone(worker.cfg.remote)
    scan(conn, rc, worker.cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)

    mkv_row = db.get_by_path(conn, "Movies/a.mkv")
    mp4_row = db.get_by_path(conn, "Movies/a.mp4")
    assert mkv_row is not None and mp4_row is not None

    db.mark(conn, mkv_row["id"], "leased", worker="pc", lease_until=time.time() + 600)

    # Even though the mp4 row may well have the higher score, it must be skipped.
    db.mark(conn, mp4_row["id"], "queued", score=1e9)

    picked = worker._next_job()
    assert picked is None or picked["id"] != mp4_row["id"]


def _budget_wait_sleep(worker):
    """Run until the first sleep and return what it asked for."""
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    worker.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    return slept[0]


@pytest.mark.parametrize("stage", ["leased", "remote_ready"])
def test_budget_wait_is_capped_while_pc_holds_work(conn, env, make_clip, stage):
    root, worker = env
    shutil.copy(make_clip("b"), root / "Movies" / "b.mkv")
    scan(conn, worker.rclone, worker.cfg,
         lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s), log=lambda s: None)
    pc_row = db.get_by_path(conn, "Movies/b.mkv")["id"]
    db.mark(conn, pc_row, stage, worker="pc", encoder="nvenc", lease_until=time.time() + 600,
            out_size=len(PC_OUT), out_md5=PC_MD5)
    worker._remote_fail_at = worker.clock()  # skip finishing the remote_ready row now
    worker.download_budget.block(worker.cfg.download_block_secs)  # a.mkv must wait
    assert worker.download_budget.seconds_until_reset() > worker.cfg.idle_poll_secs
    assert _budget_wait_sleep(worker) <= worker.cfg.idle_poll_secs


def test_budget_wait_unchanged_without_pc_work(conn, env):
    _, worker = env
    worker.download_budget.block(worker.cfg.download_block_secs)
    expected = worker.download_budget.seconds_until_reset()
    assert expected > worker.cfg.idle_poll_secs
    assert _budget_wait_sleep(worker) == pytest.approx(expected, abs=5)


def test_remote_ready_swapped_during_server_encode(conn, env, make_clip, monkeypatch):
    """The PC's remote_ready row must be verified and swapped via a tick during the
    server's own encode, not only between jobs."""
    import vidshrink.worker as worker_mod

    root, worker = env
    shutil.copy(make_clip("b"), root / "Movies" / "b.mkv")
    rc = Rclone(worker.cfg.remote)
    scan(conn, rc, worker.cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)
    pc_vid = as_remote_ready(conn, root)

    real_run_encode = worker_mod.run_encode
    ticked = []

    def wrapper(cmd, log_path, tick=None, tick_secs=60.0):
        if tick is not None:
            tick()
            ticked.append(1)
        return real_run_encode(cmd, log_path, tick=tick, tick_secs=tick_secs)

    monkeypatch.setattr(worker_mod, "run_encode", wrapper)

    done = worker.run(limit=2)

    assert ticked, "tick was never invoked during the server's own encode"
    assert done == 2
    assert db.get(conn, pc_vid)["stage"] == "done"
    assert (root / "Movies" / "a.mkv").read_bytes() == PC_OUT
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert db.get_by_path(conn, "Movies/b.mkv")["stage"] == "done"


def test_encode_tick_error_does_not_raise(conn, env, monkeypatch):
    """A tick whose _finish_remote raises unexpectedly must not escape _encode_tick
    (which run_encode calls while the server's own ffmpeg encode is still running)."""
    root, worker = env
    as_remote_ready(conn, root)

    def boom(*a, **k):
        raise RuntimeError("_finish_remote exploded")

    monkeypatch.setattr(worker, "_finish_remote", boom)

    logs = []
    worker.log = logs.append

    worker._encode_tick()  # must not raise

    assert any("PC result check during encode failed" in m for m in logs)


def test_swap_needs_attention_log_names_drive_tmp_for_pc_rows(conn, env):
    root, worker = env
    vid = as_remote_ready(conn, root)
    db.mark(conn, vid, "swapping")
    (root / "Movies" / "a.mkv").unlink()  # the original is already gone
    logs = []
    worker.log = logs.append
    worker._swap_failed(db.get(conn, vid), SwapError("move failed"))
    msg = next(m for m in logs if "NEEDS ATTENTION" in m)
    assert "Movies/a.vidshrink-tmp.mkv" in msg
    assert str(worker._out(vid)) not in msg
