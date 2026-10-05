import shutil
import threading
import time
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.encode import run_encode_cancellable
from vidshrink.jobs import JobClient, JobStore, make_server
from vidshrink.paths import md5_file
from vidshrink.pc_worker import PCConfig, PCWorker, load_pc_config
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, free_port
from vidshrink.scanner import scan
from vidshrink.worker import Worker


def x265_encode(src, out, probe, log_path, cancel):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
           "-map", "0", "-c", "copy", "-c:v", "libx265", "-preset", "ultrafast",
           "-x265-params", "log-level=error", str(out)]
    return run_encode_cancellable(cmd, log_path, cancel)


@pytest.fixture
def setup(conn, cfg, make_clip, tmp_path):
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    rc = Rclone(cfg.remote)
    scan(conn, rc, cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)
    port = free_port()
    # Deviation from the brief: sqlite3 connections from db.connect() are
    # check_same_thread=True, so the HTTP server must create its OWN connection
    # from inside the thread that runs serve_forever (which is also the thread
    # that handles every request, since HTTPServer is single-threaded) rather
    # than reuse a connection created in the fixture's (main) thread. This
    # mirrors the `job_service` fixture in tests/conftest.py. A second JobStore
    # built on the test's own `conn` is returned to the test for direct,
    # same-thread assertions (e.g. store.downloads.blocked()); both point at
    # the same db file, so writes committed by one are visible to the other.
    server_holder = {}
    ready = threading.Event()

    def run_server():
        server_conn = db.connect(cfg.db_path)
        server_store = JobStore(server_conn, cfg, Rclone(cfg.remote), log=lambda m: None)
        server = make_server(server_store, "tok", "127.0.0.1", port)
        server_holder["server"] = server
        server_holder["conn"] = server_conn
        ready.set()
        try:
            server.serve_forever()
        finally:
            server_conn.close()

    threading.Thread(target=run_server, daemon=True).start()
    assert ready.wait(timeout=10)
    store = JobStore(conn, cfg, rc, log=lambda m: None)
    pcfg = PCConfig(server=f"http://127.0.0.1:{port}", token="tok", remote=cfg.remote,
                    workdir=tmp_path / "pcwork", worker_name="pc", heartbeat_secs=0.2,
                    idle_secs=0.2, retry_base_delay=0.0)
    pc = PCWorker(pcfg, JobClient(pcfg.server, "tok"), Rclone(cfg.remote),
                  encode=x265_encode, log=lambda m: None)
    yield root, pc, store
    server_holder["server"].shutdown()
    server_holder["server"].server_close()


def run_in_thread(pc):
    t = threading.Thread(target=pc.run, daemon=True)
    t.start()
    return t


def wait_for(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_full_job_then_server_swaps(conn, cfg, setup):
    root, pc, store = setup
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    assert wait_for(lambda: pc.done == 1)
    pc.stop(now=True)
    t.join(10)
    row = db.get(conn, vid)
    tmp = root / "Movies" / "a.vidshrink-tmp.mkv"
    assert tmp.exists() and md5_file(tmp) == row["out_md5"]
    assert not any((cfg.workdir.parent / "pcwork").glob("*"))
    worker = Worker(conn, Rclone(cfg.remote), cfg, Budget(conn, cfg.daily_upload_limit),
                    sleep=lambda s: None, log=lambda s: None)
    assert worker.run(limit=1) == 1
    assert db.get(conn, vid)["stage"] == "done"


def test_stop_now_releases_job(conn, cfg, make_clip, setup):
    root, pc, store = setup
    # A longer clip so the encode is still running when we stop; rescan picks it up.
    shutil.copy(make_clip("long", seconds=120, width=1280, height=720), root / "Movies" / "a.mkv")
    scan(conn, Rclone(cfg.remote), cfg,
         lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s), log=lambda s: None)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert db.get(conn, vid)["stage"] == "queued"
    t = run_in_thread(pc)
    assert wait_for(lambda: pc.stage == "encoding")
    pc.stop(now=True)
    t.join(15)
    assert not t.is_alive()
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()


def test_stop_after_this_file_finishes_first(conn, setup):
    root, pc, store = setup
    t = run_in_thread(pc)
    assert wait_for(lambda: pc.state == "working")
    pc.stop(now=False)
    t.join(60)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert db.get(conn, vid)["stage"] == "remote_ready"
    assert pc.state == "stopped"


def test_download_quota_is_reported(conn, setup, monkeypatch):
    root, pc, store = setup

    def quota(path, local):
        raise RcloneError(1, "Error 403: downloadQuotaExceeded")

    monkeypatch.setattr(pc.rclone, "download", quota)
    t = run_in_thread(pc)
    assert wait_for(lambda: store.downloads.blocked())
    pc.stop(now=True)
    t.join(10)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert (db.get(conn, vid)["stage"], db.get(conn, vid)["attempts"]) == ("queued", 0)


def test_server_cancel_abandons_job(conn, setup):
    root, pc, store = setup
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "leased")
    db.mark(conn, vid, "skipped", worker=None, reason="operator")  # taken back, not reclaimable
    assert wait_for(lambda: pc.current_path is None, timeout=30)
    pc.stop(now=True)
    t.join(10)
    assert db.get(conn, vid)["stage"] == "skipped"
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()


def test_load_pc_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('server = "http://192.168.1.10:8765"\ntoken = "abc"\ncq = 24\n'
                 'workdir = "D:/vidshrink"\n')
    c = load_pc_config(p)
    assert (c.server, c.token, c.cq, c.bwlimit) == ("http://192.168.1.10:8765", "abc", 24, "6M")
    assert c.workdir == Path("D:/vidshrink")
