import hashlib
import shutil
import time
from pathlib import Path

import pytest

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.jobs import PC_SEEN_KEY
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone
from vidshrink.scanner import scan
from vidshrink.worker import Worker

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
