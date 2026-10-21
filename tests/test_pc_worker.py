import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.encode import EncodeCancelled, run_encode_cancellable
from vidshrink.jobs import JobClient, JobServiceError, JobStore, LeaseLost, make_server
from vidshrink.paths import md5_file
from vidshrink.pc_worker import PCConfig, PCWorker, clean_workdir, load_pc_config
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, TransferCancelled, free_port
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
    # Like the server's test cfg: no disk factor or reserve, so a tmpfs with a few
    # GB free can still hold a queue of tiny clips.
    pc.queue.disk_factor, pc.queue.disk_reserve = 0.0, 0
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
    logs = []
    pc.log = logs.append
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    assert wait_for(lambda: pc.done == 1)
    for prefix in ("downloading Movies/a.mkv", "encoding Movies/a.mkv", "verifying", "uploading"):
        assert any(m.startswith(prefix) for m in logs), (prefix, logs)
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


# --- pipeline: several jobs held at once -------------------------------------

def add_videos(conn, cfg, root, *names):
    """More copies of the setup clip under new names, scanned into the queue."""
    for name in names:
        shutil.copy(root / "Movies" / "a.mkv", root / "Movies" / f"{name}.mkv")
    scan(conn, Rclone(cfg.remote), cfg,
         lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s), log=lambda s: None)
    return [db.get_by_path(conn, f"Movies/{n}.mkv")["id"] for n in ("a", *names)]


class GatedEncode:
    """x265_encode that waits for `gate` before encoding; records each src it starts."""

    def __init__(self):
        self.gate = threading.Event()
        self.started = []

    def __call__(self, src, out, probe, log_path, cancel):
        self.started.append(int(Path(src).name.split(".")[0]))
        while not self.gate.wait(0.05):
            if cancel.is_set():
                raise EncodeCancelled("cancelled")
        return x265_encode(src, out, probe, log_path, cancel)


def test_pipeline_downloads_next_while_encoding(conn, cfg, setup):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: enc.started == [a])
        assert wait_for(lambda: (pc.cfg.workdir / f"{b}.downloaded").exists())
        assert wait_for(lambda: db.get(conn, b)["reserve_secs"] == 7 * 86400)
        assert db.get(conn, b)["stage"] == "leased"
        assert enc.started == [a]  # the first encode is still held at the gate
        enc.gate.set()
        assert wait_for(lambda: db.get(conn, a)["stage"] == "remote_ready"
                        and db.get(conn, b)["stage"] == "remote_ready")
        assert pc.done == 2
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_queue_hours_limits_prefetch(conn, cfg, setup, monkeypatch):
    from vidshrink import scorer
    root, pc, store = setup
    monkeypatch.setattr(scorer, "expected_hours", lambda *a, **k: 0.6)
    ids = add_videos(conn, cfg, root, "b", "c")
    pc.queue.queue_hours = 1.0
    enc = GatedEncode()
    pc.encode = enc
    held = []
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: len(enc.started) == 1)
        end = time.time() + 3
        while time.time() < end:
            held.append(len(pc.queue.held_ids()))
            time.sleep(0.05)
        assert max(held) == 2  # one encoding, one downloaded behind it, no more
        assert sum(db.get(conn, i)["stage"] == "queued" for i in ids) == 1
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_verifies_next_while_uploading(conn, cfg, setup, monkeypatch):
    root, pc, store = setup
    a, b, c = add_videos(conn, cfg, root, "b", "c")
    gate, uploading = threading.Event(), []
    orig_upload = pc.rclone.upload

    def gated(local, remote, cancel=None):
        uploading.append(remote)
        while not gate.wait(0.05):
            if cancel is not None and cancel.is_set():
                raise TransferCancelled("cancelled")
        return orig_upload(local, remote, cancel=cancel)

    monkeypatch.setattr(pc.rclone, "upload", gated)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: len(uploading) == 1)
        # a's upload is held; b is encoded and verified meanwhile, not after it.
        assert wait_for(lambda: pc.queue._jobs.get(b) is not None
                        and pc.queue._jobs[b].state == "verified")
        # ...but the verifier stays only one file ahead of the uploader.
        assert wait_for(lambda: pc.queue._jobs.get(c) is not None
                        and pc.queue._jobs[c].state == "encoded")
        time.sleep(1)
        assert pc.queue._jobs[c].state == "encoded"
        assert len(uploading) == 1
        gate.set()
        assert wait_for(lambda: all(db.get(conn, i)["stage"] == "remote_ready"
                                    for i in (a, b, c)))
    finally:
        gate.set()
        pc.stop(now=True)
        t.join(15)


def test_upload_backlog_stops_claiming(tmp_path):
    pc = fake_pc(tmp_path, queue_upload_gb=1e-8)  # 10 bytes
    pc.cfg.workdir.mkdir(parents=True)
    lj = pc._hold(dict(JOB, id=1, size=100, expected_hours=0.1))
    pc.queue.set_state(1, "verified")
    lj.out.write_bytes(b"y" * 11)
    assert pc._claim_filters() is None
    assert not pc._admits(dict(JOB, id=2, size=1, expected_hours=0.1))
    lj.out.write_bytes(b"y" * 10)  # at the limit, not over it
    assert pc._claim_filters() is not None
    assert pc._admits(dict(JOB, id=2, size=1, expected_hours=0.1))


def test_queue_disk_limits_prefetch(conn, cfg, setup, monkeypatch):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    size = (root / "Movies" / "a.mkv").stat().st_size
    pc.queue.queue_disk_bytes = int(size * 1.5)  # room for one source, not two
    enc = GatedEncode()
    pc.encode = enc
    downloads = []
    orig_download = pc.download_rclone.download

    def spy(path, local, cancel=None):
        downloads.append(path)
        return orig_download(path, local, cancel=cancel)

    monkeypatch.setattr(pc.download_rclone, "download", spy)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: enc.started == [a])
        time.sleep(2)
        assert downloads == ["Movies/a.mkv"]
        assert pc.queue.held_ids() == {a}
        enc.gate.set()
        assert wait_for(lambda: db.get(conn, b)["stage"] == "remote_ready")
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_heartbeat_many_covers_all_held_jobs(conn, cfg, setup, monkeypatch):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    beats = []
    orig = pc.client.heartbeat_many

    def spy(ids, worker, download_account="main"):
        beats.append(set(ids))
        return orig(ids, worker, download_account=download_account)

    def single(*a, **k):
        raise AssertionError("single-job heartbeat must not be used")

    monkeypatch.setattr(pc.client, "heartbeat_many", spy)
    monkeypatch.setattr(pc.client, "heartbeat", single)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: {a, b} in beats)
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_server_cancel_drops_only_that_job(conn, cfg, setup):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: enc.started == [a])
        assert wait_for(lambda: (pc.cfg.workdir / f"{b}.downloaded").exists())
        db.mark(conn, b, "skipped", worker=None, reason="operator")
        assert wait_for(lambda: b not in pc.queue.held_ids())
        assert not list(pc.cfg.workdir.glob(f"{b}.*"))
        assert a in pc.queue.held_ids() and pc.stage == "encoding"
        enc.gate.set()
        assert wait_for(lambda: db.get(conn, a)["stage"] == "remote_ready")
        assert db.get(conn, b)["stage"] == "skipped"
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_upload_limited_keeps_encoded_files(conn, setup, monkeypatch):
    root, pc, store = setup
    uploads = []

    def limited(local, path, cancel=None):
        uploads.append(path)
        raise RcloneError(1, "googleapi: Error 403: User rate limit exceeded.,"
                          " userRateLimitExceeded")

    outcomes = []
    orig_report = pc.client.report

    def spy(job_id, worker, outcome, **data):
        outcomes.append(outcome)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.rclone, "upload", limited)
    monkeypatch.setattr(pc.client, "report", spy)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: store.uploads.blocked())
        assert wait_for(lambda: "upload_paused" in outcomes)
        time.sleep(1)
        row = db.get(conn, vid)
        assert (row["stage"], row["worker"]) == ("leased", "pc")
        assert (pc.cfg.workdir / f"{vid}.out.mkv").exists()
        assert (pc.cfg.workdir / f"{vid}.encoded").exists()
        assert "upload_limited" not in outcomes
        assert len(uploads) == 1  # no new upload attempt while paused
    finally:
        pc.stop(now=True)
        t.join(15)


def test_stop_after_during_upload_pause_keeps_encoded_reserved(conn, setup, monkeypatch):
    root, pc, store = setup

    def limited(local, path, cancel=None):
        raise RcloneError(1, "googleapi: Error 403: User rate limit exceeded.,"
                          " userRateLimitExceeded")

    released = []
    orig_release = pc.client.release
    monkeypatch.setattr(pc.rclone, "upload", limited)
    monkeypatch.setattr(pc.client, "release", lambda job_id, worker: (
        released.append(job_id), orig_release(job_id, worker))[1])
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    t = run_in_thread(pc)
    assert wait_for(lambda: "Uploads paused" in pc.status_line())
    pc.stop(now=False)
    t.join(15)  # does not wait out the 6 h pause
    assert not t.is_alive()
    assert released == []
    assert_reserved(conn, vid)
    for ext in (".job.json", ".out.mkv", ".encoded"):
        assert (pc.cfg.workdir / f"{vid}{ext}").exists(), ext


def test_server_cancel_during_encode_other_job_completes(conn, cfg, setup):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: enc.started == [a])
        assert wait_for(lambda: (pc.cfg.workdir / f"{b}.downloaded").exists())
        db.mark(conn, a, "skipped", worker=None, reason="operator")
        assert wait_for(lambda: a not in pc.queue.held_ids())
        assert wait_for(lambda: not list(pc.cfg.workdir.glob(f"{a}.*")))
        enc.gate.set()
        assert wait_for(lambda: db.get(conn, b)["stage"] == "remote_ready")
        assert enc.started == [a, b]
        assert db.get(conn, a)["stage"] == "skipped"
        assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    finally:
        enc.gate.set()
        pc.stop(now=True)
        t.join(15)


def test_status_line_shows_every_stage(tmp_path):
    pc = fake_pc(tmp_path)
    pc.cfg.workdir.mkdir(parents=True)
    for jid, name, state in ((1, "X", "encoding"), (2, "W1", "downloaded"),
                             (3, "W2", "downloaded"), (4, "Y", "uploading"),
                             (5, "Z", "downloading"), (6, "V", "verifying"),
                             (7, "E", "encoded"), (8, "F", "verified")):
        pc._hold(dict(JOB, id=jid, path=f"Movies/{name}.mkv", size=1))
        pc.queue.set_state(jid, state)
    assert pc.status_line() == ("Encoding X.mkv (2 waiting) · Downloading Z.mkv"
                                " · Verifying V.mkv · Uploading Y.mkv · 2 encoded waiting")
    assert (pc.stage, pc.current_path) == ("encoding", "Movies/X.mkv")


def assert_reserved(conn, vid):
    row = db.get(conn, vid)
    assert (row["stage"], row["worker"], row["attempts"]) == ("leased", "pc", 0)
    assert row["reserve_secs"] == 7 * 86400


def test_stop_now_keeps_downloaded_jobs_reserved(conn, cfg, setup):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    released = []
    orig_release = pc.client.release
    pc.client.release = lambda job_id, worker: (released.append(job_id),
                                                orig_release(job_id, worker))[1]
    t = run_in_thread(pc)
    assert wait_for(lambda: enc.started == [a])
    assert wait_for(lambda: db.get(conn, b)["reserve_secs"] == 7 * 86400)
    (pc.cfg.workdir / f"{a}.out.mkv").write_bytes(b"partial encode")
    pc.stop(now=True)
    t.join(15)
    assert not t.is_alive()
    assert released == []
    for vid in (a, b):
        assert_reserved(conn, vid)
        for ext in (".job.json", ".src", ".downloaded"):
            assert (pc.cfg.workdir / f"{vid}{ext}").exists(), (vid, ext)
    assert not (pc.cfg.workdir / f"{a}.out.mkv").exists()  # the cancelled encode's output
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()


def test_stop_now_releases_unfinished_download(conn, setup, monkeypatch):
    root, pc, store = setup
    started = threading.Event()

    def slow_download(path, local, cancel=None):
        Path(local).write_bytes(b"part")
        started.set()
        while not cancel.wait(0.05):
            pass
        raise TransferCancelled("cancelled")

    monkeypatch.setattr(pc.download_rclone, "download", slow_download)
    t = run_in_thread(pc)
    assert started.wait(30)
    pc.stop(now=True)
    t.join(15)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert (db.get(conn, vid)["stage"], db.get(conn, vid)["attempts"]) == ("queued", 0)
    assert not list(pc.cfg.workdir.glob(f"{vid}.*"))


def test_stop_after_drains_uploads_keeps_downloaded(conn, cfg, setup):
    root, pc, store = setup
    a, b = add_videos(conn, cfg, root, "b")
    enc = GatedEncode()
    pc.encode = enc
    t = run_in_thread(pc)
    assert wait_for(lambda: enc.started == [a])
    assert wait_for(lambda: db.get(conn, b)["reserve_secs"] == 7 * 86400)
    pc.stop(now=False)
    assert pc.state == "finishing"
    enc.gate.set()
    t.join(60)
    assert not t.is_alive()
    assert db.get(conn, a)["stage"] == "remote_ready"  # finished, then uploaded
    assert pc.done == 1
    assert enc.started == [a]
    assert_reserved(conn, b)  # never encoded: kept on disk, still reserved
    for ext in (".job.json", ".src", ".downloaded"):
        assert (pc.cfg.workdir / f"{b}{ext}").exists()
    assert not list(pc.cfg.workdir.glob(f"{a}.*"))
    assert pc.state == "stopped"


def test_stop_after_while_claim_in_flight_downloads_nothing(conn, setup, monkeypatch):
    root, pc, store = setup
    downloads = []
    monkeypatch.setattr(pc.download_rclone, "download",
                        lambda *a, **k: downloads.append(a))
    orig_admits = pc._admits

    def stop_meanwhile(job):
        # Stop after lands after the downloader's own stop check, before it holds the job.
        pc.stop(now=False)
        return orig_admits(job)

    monkeypatch.setattr(pc, "_admits", stop_meanwhile)
    t = run_in_thread(pc)
    t.join(30)
    assert not t.is_alive()
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert downloads == []
    assert (db.get(conn, vid)["stage"], db.get(conn, vid)["attempts"]) == ("queued", 0)
    assert not list(pc.cfg.workdir.glob(f"{vid}.*"))


def test_download_quota_is_reported(conn, setup, monkeypatch):
    root, pc, store = setup

    def quota(path, local, cancel=None):
        raise RcloneError(1, "Error 403: downloadQuotaExceeded")

    monkeypatch.setattr(pc.rclone, "download", quota)
    t = run_in_thread(pc)
    assert wait_for(lambda: store.downloads.blocked())
    pc.stop(now=True)
    t.join(10)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert (db.get(conn, vid)["stage"], db.get(conn, vid)["attempts"]) == ("queued", 0)


def test_server_cancel_abandons_job(conn, cfg, make_clip, setup, monkeypatch):
    root, pc, store = setup
    # A longer, slow-to-encode clip: the heartbeat (every 0.2s) must observe the
    # cancel well before the job could otherwise finish and upload, so this
    # provably takes the heartbeat-cancel path rather than racing it.
    shutil.copy(make_clip("long", seconds=120, width=1280, height=720), root / "Movies" / "a.mkv")
    scan(conn, Rclone(cfg.remote), cfg,
         lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s), log=lambda s: None)
    outcomes = []
    orig_report = pc.client.report

    def spy(job_id, worker, outcome, **data):
        outcomes.append(outcome)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", spy)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "leased")
    db.mark(conn, vid, "skipped", worker=None, reason="operator")  # taken back, not reclaimable
    assert wait_for(lambda: pc.current_path is None, timeout=30)
    pc.stop(now=True)
    t.join(15)
    assert db.get(conn, vid)["stage"] == "skipped"
    assert not (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
    assert "uploaded" not in outcomes


def test_missing_tool_run_claims_nothing(setup, monkeypatch):
    root, pc, store = setup
    monkeypatch.setattr(shutil, "which", lambda name, **k: None)
    claimed = []
    orig_claim = pc.client.claim

    def spy(*a, **k):
        claimed.append(1)
        return orig_claim(*a, **k)

    monkeypatch.setattr(pc.client, "claim", spy)
    pc.run()
    assert pc.state == "stopped"
    assert claimed == []


def test_local_fault_during_job_releases_and_stops(conn, setup):
    root, pc, store = setup

    def bad_encode(src, out, probe, log_path, cancel):
        raise OSError("disk full")

    pc.encode = bad_encode
    t = run_in_thread(pc)
    t.join(15)
    assert not t.is_alive()
    assert pc.state == "stopped"
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)


def test_drive_login_failure_releases_and_stops(conn, setup, monkeypatch):
    """An expired/revoked Drive login is the PC's problem, not the video's: release
    the job without using an attempt and stop, instead of failing the whole queue."""
    root, pc, store = setup
    calls = {"n": 0}

    def expired(*a, **k):
        calls["n"] += 1
        raise RcloneError(1, 'CRITICAL: Failed to create file system for "gdrive-pc:Movies/a.mkv": '
                             "couldn't fetch token: invalid_grant: maybe token expired?")

    monkeypatch.setattr(pc.download_rclone, "download", expired)
    t = run_in_thread(pc)
    t.join(30)
    assert not t.is_alive()
    assert pc.state == "stopped"
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)
    assert calls["n"] == 1  # no pointless retries of a dead login


def test_unexpected_exception_in_job_releases_and_continues(conn, setup, monkeypatch):
    root, pc, store = setup
    calls = {"n": 0}

    def boom(src, out, probe, log_path, cancel):
        calls["n"] += 1
        raise RuntimeError("kaboom")

    pc.encode = boom
    released = []
    orig_release = pc.client.release

    def spy_release(job_id, worker):
        released.append(job_id)
        return orig_release(job_id, worker)

    monkeypatch.setattr(pc.client, "release", spy_release)
    reported = []
    orig_report = pc.client.report

    def spy_report(job_id, worker, outcome, **data):
        if outcome != "downloaded":
            reported.append((calls["n"], outcome))
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", spy_report)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: calls["n"] >= 1)
    assert wait_for(lambda: released == [vid])  # the release actually happened
    assert wait_for(lambda: calls["n"] >= 2)  # the loop survived and claimed it again
    assert not [r for r in reported if r[0] < 2]  # the first error burned no attempt
    pc.stop(now=True)
    t.join(10)
    assert not t.is_alive()


def test_report_retries_on_server_blip(conn, setup, monkeypatch):
    root, pc, store = setup
    orig_report = pc.client.report
    calls = {"n": 0}

    def flaky(job_id, worker, outcome, **data):
        if outcome == "uploaded":
            calls["n"] += 1
            if calls["n"] == 1:
                raise JobServiceError("simulated blip", status=500)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", flaky)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    pc.stop(now=True)
    t.join(10)
    assert calls["n"] >= 2


def test_stop_now_during_upload_keeps_encoded_and_cleans_tmp(conn, setup, monkeypatch):
    root, pc, store = setup
    tmp_on_remote = root / "Movies" / "a.vidshrink-tmp.mkv"

    # A deterministic stand-in for a slow transfer: actually copies the file to
    # the remote tmp path first (as a real upload would, even a partial one),
    # then blocks, polling `cancel` the way Rclone._run does, until Stop now
    # fires, then raises TransferCancelled.
    def slow_upload(local, path, cancel=None):
        shutil.copy(local, tmp_on_remote)
        for _ in range(200):
            if cancel is not None and cancel.is_set():
                raise TransferCancelled("cancelled")
            time.sleep(0.05)
        raise AssertionError("upload was not cancelled in time")

    monkeypatch.setattr(pc.rclone, "upload", slow_upload)
    t = run_in_thread(pc)
    assert wait_for(lambda: tmp_on_remote.exists(), timeout=30)
    pc.stop(now=True)
    t.join(15)
    assert not t.is_alive()
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert_reserved(conn, vid)
    assert not tmp_on_remote.exists()
    for ext in (".job.json", ".out.mkv", ".encoded"):
        assert (pc.cfg.workdir / f"{vid}{ext}").exists(), ext


def test_ambiguous_uploaded_then_retry_succeeds_idempotently_tmp_kept(conn, setup, monkeypatch):
    """The first 'uploaded' call actually commits server-side (the real report()
    runs), but the PC never sees the response (simulated: it raises right after).
    The retry must succeed idempotently (the server-side idempotency from item A)
    and the tmp must NOT be deleted."""
    root, pc, store = setup
    tmp_on_remote = root / "Movies" / "a.vidshrink-tmp.mkv"
    orig_report = pc.client.report
    calls = {"n": 0}

    def flaky(job_id, worker, outcome, **data):
        if outcome != "uploaded":
            return orig_report(job_id, worker, outcome, **data)
        calls["n"] += 1
        if calls["n"] == 1:
            orig_report(job_id, worker, outcome, **data)  # really commits server-side
            raise JobServiceError("dropped connection after commit", status=None)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", flaky)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    pc.stop(now=True)
    t.join(10)
    assert calls["n"] >= 2
    assert tmp_on_remote.exists()
    row = db.get(conn, vid)
    assert row["out_md5"] == md5_file(tmp_on_remote)


def test_ambiguous_uploaded_then_lease_lost_tmp_not_deleted(conn, setup, monkeypatch):
    """An ambiguous failure (we don't know if the server committed) followed by a
    definite LeaseLost must NOT delete the tmp: the earlier ambiguity means a
    definite-looking 409 doesn't prove it's safe (the server may have actually
    accepted the first attempt and moved on for an unrelated reason by the time
    the second one lands)."""
    root, pc, store = setup
    tmp_on_remote = root / "Movies" / "a.vidshrink-tmp.mkv"
    calls = {"n": 0}

    orig_report = pc.client.report

    def flaky(job_id, worker, outcome, **data):
        if outcome == "downloaded":
            return orig_report(job_id, worker, outcome, **data)
        if outcome != "uploaded":
            raise AssertionError("unexpected outcome in this test")
        calls["n"] += 1
        if calls["n"] == 1:
            raise JobServiceError("dropped connection, unknown outcome", status=None)
        raise LeaseLost("someone else has it now")

    monkeypatch.setattr(pc.client, "report", flaky)
    t = run_in_thread(pc)
    assert wait_for(lambda: tmp_on_remote.exists(), timeout=30)
    assert wait_for(lambda: calls["n"] >= 2, timeout=30)
    pc.stop(now=True)
    t.join(10)
    assert tmp_on_remote.exists()  # never deleted despite the later LeaseLost


def test_stop_now_during_transfer_retry_releases_not_failed(conn, setup, monkeypatch):
    root, pc, store = setup
    pc.cfg.retry_base_delay = 5.0  # long enough that Stop now clearly lands mid-wait
    attempts = {"n": 0}

    def flaky_download(path, local, cancel=None):
        attempts["n"] += 1
        raise RcloneError(1, "connection reset by peer")

    monkeypatch.setattr(pc.rclone, "download", flaky_download)
    outcomes = []
    orig_report = pc.client.report

    def spy(job_id, worker, outcome, **data):
        outcomes.append(outcome)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", spy)
    t = run_in_thread(pc)
    assert wait_for(lambda: attempts["n"] >= 1)
    pc.stop(now=True)
    t.join(10)
    assert not t.is_alive()
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)
    assert "failed" not in outcomes


def test_poison_job_reported_failed_on_second_unexpected_error(conn, setup, monkeypatch):
    root, pc, store = setup
    calls = {"n": 0}
    events = []  # (encode calls so far, "release" or the reported outcome)

    def boom(src, out, probe, log_path, cancel):
        calls["n"] += 1
        raise TypeError("always broken")

    orig_release, orig_report = pc.client.release, pc.client.report

    def spy_release(job_id, worker):
        events.append((calls["n"], "release"))
        return orig_release(job_id, worker)

    def spy_report(job_id, worker, outcome, **data):
        if outcome != "downloaded":
            events.append((calls["n"], outcome))
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "release", spy_release)
    monkeypatch.setattr(pc.client, "report", spy_report)
    pc.encode = boom
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["attempts"] == 1)
    # First: released un-penalised. Second, right after re-claiming: reported failed.
    assert events[:2] == [(1, "release"), (2, "failed")]
    assert db.get(conn, vid)["stage"] != "failed"  # max_attempts=2, so still queued
    pc.stop(now=True)
    t.join(10)
    assert not t.is_alive()


def test_rate_limited_download_is_transient_not_upload_limited(conn, setup, monkeypatch):
    root, pc, store = setup
    pc.cfg.idle_secs = 0.1  # fast backoff for the test

    def limited(path, local, cancel=None):
        raise RcloneError(1, "googleapi: Error 403: User rate limit exceeded.,"
                          " userRateLimitExceeded")

    monkeypatch.setattr(pc.rclone, "download", limited)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "queued"
                    and db.get(conn, vid)["attempts"] == 0)
    assert not store.uploads.blocked()
    pc.stop(now=True)
    t.join(10)
    assert not t.is_alive()


def test_ffmpeg_dir_added_to_path_once(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    cfg1 = PCConfig(server="http://x", token="t", ffmpeg_dir="/opt/ffmpeg")
    PCWorker(cfg1, client=None, rclone=None, encode=lambda *a: 0.0)
    PCWorker(cfg1, client=None, rclone=None, encode=lambda *a: 0.0)
    assert os.environ["PATH"].split(os.pathsep).count("/opt/ffmpeg") == 1


def test_load_pc_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('server = "http://192.168.1.10:8765"\ntoken = "abc"\ncq = 24\n'
                 'workdir = "D:/vidshrink"\n')
    c = load_pc_config(p)
    assert (c.server, c.token, c.cq, c.bwlimit) == ("http://192.168.1.10:8765", "abc", 24, "6M")
    assert c.workdir == Path("D:/vidshrink")


def test_load_pc_config_cloud_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('server = "http://s"\ntoken = "abc"\nencoder = "x265-cloud"\n'
                 'download_account = "cloud"\nmax_hours = 24.0\ndisk_check = true\n'
                 'bwlimit = ""\n')
    c = load_pc_config(p)
    assert (c.encoder, c.download_account, c.max_hours, c.disk_check, c.bwlimit) == (
        "x265-cloud", "cloud", 24.0, True, "")


# --- final-review fixes: unit tests with fakes ------------------------------

class FakeRemote:
    """Just enough of Rclone for the tmp-cleanup paths."""

    def __init__(self, files=None):
        self.files = dict(files or {})  # path -> md5
        self.deleted = []
        self.remote = "gdrive:"

    def exists(self, path):
        return path in self.files

    def md5(self, path):
        return self.files.get(path)

    def delete(self, path):
        self.deleted.append(path)
        self.files.pop(path, None)


class FakeClient:
    def __init__(self, heartbeat=None, claim=None):
        self._heartbeat = heartbeat
        self._claim = claim
        self.released = []
        self.claims = []
        self.alerts = []
        self.reports = []
        self.beats = []

    def heartbeat(self, job_id, worker, download_account="main"):
        raise AssertionError("a pipelined worker sends heartbeat_many")

    def heartbeat_many(self, ids, worker, download_account="main"):
        self.beats.append(list(ids))
        return self._heartbeat()

    def claim(self, worker, encoder="nvenc", download_account="main", resume=False,
              free_bytes=None, max_hours=None):
        self.claims.append(dict(worker=worker, encoder=encoder,
                                download_account=download_account, resume=resume,
                                free_bytes=free_bytes,
                                max_hours=max_hours))
        return self._claim()

    def alert(self, worker, message):
        self.alerts.append((worker, message))

    def report(self, job_id, worker, outcome, download_account="main", **data):
        self.reports.append(outcome)

    def release(self, job_id, worker):
        self.released.append(job_id)


JOB = {"id": 7, "path": "Movies/a.mkv", "tmp_path": "Movies/a.vidshrink-tmp.mkv"}
TMP = JOB["tmp_path"]


def fake_pc(tmp_path, client=None, remote=None, logs=None, **cfg):
    pcfg = PCConfig(server="http://x", token="t", workdir=tmp_path / "pcwork",
                    worker_name="pc", **cfg)
    return PCWorker(pcfg, client or FakeClient(), remote or FakeRemote(),
                    encode=lambda *a: 0.0, log=(logs.append if logs is not None else
                                                (lambda m: None)))


def test_stop_now_abandon_without_local_md5_deletes_nothing(tmp_path):
    remote, logs = FakeRemote({TMP: "f" * 32}), []
    pc = fake_pc(tmp_path, remote=remote, logs=logs)
    pc._abandon(JOB, TMP, None)
    assert remote.deleted == []
    assert any("not deleting" in m and TMP in m for m in logs)
    assert pc.client.released == [7]


def test_stop_now_abandon_leaves_a_tmp_that_is_not_ours(tmp_path):
    remote = FakeRemote({TMP: "a" * 32})
    pc = fake_pc(tmp_path, remote=remote)
    pc._abandon(JOB, TMP, "b" * 32)
    assert remote.deleted == []


def test_stop_now_abandon_deletes_our_complete_tmp(tmp_path):
    remote = FakeRemote({TMP: "a" * 32})
    pc = fake_pc(tmp_path, remote=remote)
    pc._abandon(JOB, TMP, "a" * 32)
    assert remote.deleted == [TMP]


def hold(pc, job_id, state, reserved=False):
    """Put a job into pc's local queue as if a stage had brought it to `state`."""
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    lj = pc._hold(dict(JOB, id=job_id, path=f"Movies/{job_id}.mkv", size=1))
    pc.queue.set_state(job_id, state)
    if reserved:
        pc._reserved.add(job_id)
    return lj


def run_heartbeat(pc):
    stop = threading.Event()
    t = threading.Thread(target=pc._heartbeat, args=(stop,), daemon=True)
    t.start()
    return stop, t


def test_heartbeat_outage_longer_than_lease_cancels_as_server_cancel(tmp_path):
    def down():
        raise JobServiceError("cannot reach job service")

    pc = fake_pc(tmp_path, client=FakeClient(heartbeat=down), heartbeat_secs=0.02,
                 lease_secs=0.3)
    hold(pc, 7, "downloading")  # on the plain lease, inside a stage
    hold(pc, 8, "downloaded")   # on the plain lease (its report failed), waiting
    hold(pc, 9, "downloaded", reserved=True)
    cancel7 = pc._cancels[7]
    start = time.monotonic()
    stop, t = run_heartbeat(pc)
    assert wait_for(lambda: cancel7.is_set() and 8 not in pc.queue.held_ids(), timeout=5)
    assert time.monotonic() - start >= 0.3
    assert 7 in pc.queue.held_ids()  # its stage drops it, through _abandon
    assert not list(pc.cfg.workdir.glob("8.*"))
    assert 9 in pc.queue.held_ids() and not pc._cancels[9].is_set()  # reserved for days
    stop.set()
    t.join(5)


def test_heartbeat_outage_resets_on_success(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 3:  # fail twice, succeed once, ...
            raise JobServiceError("blip")
        return {"cancel": False}

    pc = fake_pc(tmp_path, client=FakeClient(heartbeat=flaky), heartbeat_secs=0.02,
                 lease_secs=0.15)
    hold(pc, 7, "downloading")
    stop, t = run_heartbeat(pc)
    time.sleep(0.6)
    stop.set()
    t.join(5)
    assert calls["n"] >= 6
    assert not pc._cancels[7].is_set() and 7 in pc.queue.held_ids()


def test_heartbeat_sends_nothing_with_no_held_job(tmp_path):
    pc = fake_pc(tmp_path, client=FakeClient(heartbeat=lambda: {"cancel": []}),
                 heartbeat_secs=0.02)
    stop, t = run_heartbeat(pc)
    time.sleep(0.2)
    assert pc.client.beats == []
    hold(pc, 7, "encoding")
    hold(pc, 8, "downloaded")
    assert wait_for(lambda: [7, 8] in pc.client.beats, timeout=5)
    stop.set()
    t.join(5)


def test_server_cancel_as_stage_finishes_forgets_promptly(tmp_path):
    pc = fake_pc(tmp_path)
    lj = hold(pc, 7, "encoding")
    (pc.cfg.workdir / "7.out.mkv").write_bytes(b"x")
    pc._server_cancel(7, "server cancelled the job")
    assert 7 in pc.queue.held_ids()  # still inside its stage
    assert pc._set_state(lj, "encoded") is False  # the stage finishes just then
    assert 7 not in pc.queue.held_ids() and 7 not in pc._jobs
    assert not list(pc.cfg.workdir.glob("7.*"))
    lj8 = hold(pc, 8, "encoding")
    pc._server_cancel(8, "server cancelled the job")
    pc._push(pc._to_upload, lj8)  # already queued when the cancel lands
    assert pc._take(pc._to_upload, "uploading") is None
    assert 8 not in pc._jobs and not list(pc.cfg.workdir.glob("8.*"))


def test_forget_deletes_files_outside_the_lock(tmp_path, monkeypatch):
    pc = fake_pc(tmp_path)
    lj = hold(pc, 7, "downloaded")
    could_lock = []
    orig_forget = pc.queue.forget

    def spy(job_id):
        def try_lock():
            if pc._cv.acquire(timeout=1):
                pc._cv.release()
                could_lock.append(True)
            else:
                could_lock.append(False)

        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        return orig_forget(job_id)

    monkeypatch.setattr(pc.queue, "forget", spy)
    pc._forget(lj)
    hold(pc, 8, "downloaded")
    pc._server_cancel(8, "server cancelled the job")
    assert could_lock == [True, True]
    assert not list(pc.cfg.workdir.glob("[78].*"))


def test_lease_secs_default_matches_server():
    from vidshrink.config import Config
    assert PCConfig(server="x", token="t").lease_secs == Config().lease_secs == 1800


def test_download_today_from_claim_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    pc = None

    def claim():
        pc.stop(now=False)
        return {"job": None, "retry_after": 0.01, "download_used": 12e9,
                "download_limit": 500e9}

    pc = fake_pc(tmp_path, client=FakeClient(
        claim=claim, heartbeat=lambda: {"cancel": [], "download_used": 13e9,
                                        "download_limit": 500e9}), heartbeat_secs=0.02)
    assert pc.download_today is None
    pc.run()
    assert pc.download_today == (12e9, 500e9)
    hold(pc, 7, "encoding")
    stop, t = run_heartbeat(pc)
    assert wait_for(lambda: pc.download_today == (13e9, 500e9), timeout=5)
    stop.set()
    t.join(5)


def test_download_today_uncapped_has_no_limit(tmp_path):
    # An uncapped account's reply carries download_used but no download_limit.
    pc = fake_pc(tmp_path)
    pc._note_download_today({"job": None, "download_used": 12e9})
    assert pc.download_today == (12e9, None)
    pc._note_download_today({"cancel": []})
    assert pc.download_today == (12e9, None)


def test_download_account_defaults_to_main_without_download_remote(tmp_path):
    pc = fake_pc(tmp_path)
    assert pc.download_account == "main"
    assert pc.download_rclone is pc.rclone


def test_download_rclone_falls_back_to_rclone_when_not_passed(tmp_path):
    remote = FakeRemote()
    pcfg = PCConfig(server="http://x", token="t", workdir=tmp_path / "w", worker_name="pc",
                    download_remote="gdrive-pc:")
    pc = PCWorker(pcfg, FakeClient(), remote, encode=lambda *a: 0.0)
    assert pc.download_account == "pc"
    assert pc.download_rclone is remote  # no download_rclone given: falls back to rclone


def test_pc_download_rclone_uses_download_remote_or_remote():
    from vidshrink.pc_worker import pc_download_rclone

    plain = PCConfig(server="x", token="t")
    assert pc_download_rclone(plain).remote == plain.remote

    pc_account = PCConfig(server="x", token="t", download_remote="gdrive-pc:")
    assert pc_download_rclone(pc_account).remote == "gdrive-pc:"


def test_pc_download_account_uses_download_rclone_not_rclone(conn, cfg, make_clip, tmp_path):
    """With download_remote set, downloads must go through download_rclone, while
    upload/md5/exists/delete stay on rclone (the main account), and claim sends
    download_account="pc"."""
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    rc = Rclone(cfg.remote)
    scan(conn, rc, cfg, lambda p, s: parse_ffprobe(run_ffprobe(str(root / p)), s),
         log=lambda s: None)

    pc_root = tmp_path / "pcdrive"
    (pc_root / "Movies").mkdir(parents=True)
    shutil.copy(root / "Movies" / "a.mkv", pc_root / "Movies" / "a.mkv")

    port = free_port()
    server_holder = {}
    ready = threading.Event()

    def run_server():
        server_conn = db.connect(cfg.db_path)
        server_store = JobStore(server_conn, cfg, Rclone(cfg.remote), log=lambda m: None)
        server = make_server(server_store, "tok", "127.0.0.1", port)
        server_holder["server"] = server
        ready.set()
        try:
            server.serve_forever()
        finally:
            server_conn.close()

    threading.Thread(target=run_server, daemon=True).start()
    assert ready.wait(timeout=10)

    main_rc = Rclone(cfg.remote)
    dl_rc = Rclone(str(pc_root) + "/")
    main_calls, dl_calls = [], []
    orig_main_dl, orig_dl_dl = main_rc.download, dl_rc.download
    main_rc.download = lambda *a, **k: (main_calls.append(a), orig_main_dl(*a, **k))[1]
    dl_rc.download = lambda *a, **k: (dl_calls.append(a), orig_dl_dl(*a, **k))[1]

    pcfg = PCConfig(server=f"http://127.0.0.1:{port}", token="tok", remote=cfg.remote,
                    download_remote=str(pc_root) + "/", workdir=tmp_path / "pcwork",
                    worker_name="pc", heartbeat_secs=0.2, idle_secs=0.2, retry_base_delay=0.0)
    client = JobClient(pcfg.server, "tok")
    claimed_accounts = []
    orig_claim = client.claim
    client.claim = lambda *a, **k: (claimed_accounts.append(k.get("download_account")),
                                    orig_claim(*a, **k))[1]
    pc = PCWorker(pcfg, client, main_rc, download_rclone=dl_rc, encode=x265_encode,
                 log=lambda m: None)
    assert pc.download_account == "pc"
    t = run_in_thread(pc)
    try:
        vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    finally:
        pc.stop(now=True)
        t.join(10)
        server_holder["server"].shutdown()
        server_holder["server"].server_close()

    assert dl_calls and not main_calls  # downloads used download_rclone only
    assert "pc" in claimed_accounts


def test_missing_output_after_encode_is_reported_failed(conn, setup, monkeypatch):
    root, pc, store = setup
    pc.encode = lambda src, out, probe, log_path, cancel: 0.0  # "exit 0", no output
    reports = []
    orig_report = pc.client.report

    def spy(job_id, worker, outcome, **data):
        if outcome != "downloaded":
            reports.append((outcome, data.get("reason", "")))
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", spy)
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: db.get(conn, vid)["attempts"] == 1)
    assert reports[0][0] == "failed" and "missing work file" in reports[0][1]
    assert t.is_alive() and pc.state != "stopped"  # not a local fault: worker carries on
    pc.stop(now=True)
    t.join(10)
    assert not t.is_alive()


def test_bwlimit_plain_caps_uploads_only_and_pair_passes_through(tmp_path):
    from vidshrink.pc_worker import pc_download_rclone, pc_rclone
    for bw, want in (("6M", "6M:off"), ("6M:40M", "6M:40M")):
        c = PCConfig(server="http://x", token="t", workdir=tmp_path, bwlimit=bw)
        for rc in (pc_rclone(c), pc_download_rclone(c)):
            args = list(rc.extra_args)
            assert args[args.index("--bwlimit") + 1] == want


# --- clean_workdir -----------------------------------------------------------

def test_clean_workdir_deletes_matching_keeps_kept_and_unrelated(tmp_path):
    keep = tmp_path / "5.src"
    keep.write_text("keep")
    other_src = tmp_path / "6.src"
    other_src.write_text("x")
    other_out = tmp_path / "6.out.mkv"
    other_out.write_text("x")
    other_log = tmp_path / "6.log"
    other_log.write_text("x")
    other_marker = tmp_path / "6.encoded"
    other_marker.write_text("1.0")
    other_dl_marker = tmp_path / "6.downloaded"
    other_dl_marker.write_text("12345")
    encoded_tmp = tmp_path / "6.encoded.tmp"
    encoded_tmp.write_text("1.0")
    downloaded_tmp = tmp_path / "6.downloaded.tmp"
    downloaded_tmp.write_text("12345")
    partial = tmp_path / "6.src.ab12cd.partial"
    partial.write_text("x")
    out_partial = tmp_path / "6.out.mkv.deadbeef.partial"
    out_partial.write_text("x")
    cal1 = tmp_path / "cal-foo.mkv"
    cal1.write_text("x")
    cal_log = tmp_path / "cal.log"
    cal_log.write_text("x")
    notes = tmp_path / "notes.txt"
    notes.write_text("keep me")
    weird = tmp_path / "123abc.src"
    weird.write_text("keep me too")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "7.src").write_text("never touched")

    deleted = clean_workdir(tmp_path, keep_ids={5})

    assert set(deleted) == {other_src, other_out, other_log, other_marker,
                            other_dl_marker, encoded_tmp, downloaded_tmp,
                            partial, out_partial, cal1, cal_log}
    assert keep.exists()
    assert notes.exists() and weird.exists()
    assert subdir.exists() and (subdir / "7.src").exists()


def test_clean_workdir_keep_ids_empty_deletes_all_matching(tmp_path):
    a = tmp_path / "1.src"
    a.write_text("x")
    b = tmp_path / "2.out.mkv"
    b.write_text("x")
    deleted = clean_workdir(tmp_path, keep_ids=set())
    assert set(deleted) == {a, b}
    assert not a.exists() and not b.exists()


def test_clean_workdir_oserror_on_one_file_does_not_block_others(tmp_path, monkeypatch):
    bad = tmp_path / "1.src"
    bad.write_text("x")
    good = tmp_path / "2.src"
    good.write_text("x")
    orig_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "1.src":
            raise OSError("permission denied")
        return orig_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    deleted = clean_workdir(tmp_path, keep_ids=set())
    assert deleted == [good]
    assert bad.exists() and not good.exists()


def test_clean_workdir_missing_dir_returns_empty(tmp_path):
    assert clean_workdir(tmp_path / "nope", keep_ids=set()) == []


# --- resume: reusing finished local work --------------------------------------

def leave_job(conn, store, pc, root, vid, state, worker="pc"):
    """A job a previous run left in pc's workdir: claimed (by `worker`), with its
    .job.json and, for "downloaded"/"encoded", its finished files and markers."""
    job = store.claim(worker, "nvenc")["job"]
    assert job["id"] == vid
    wd = pc.cfg.workdir
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{vid}.job.json").write_text(json.dumps(job))
    src = root / job["path"]
    if state == "partial":
        (wd / f"{vid}.src").write_bytes(b"part of a download")
        return job
    store.report(vid, worker, "downloaded", reserve_secs=7 * 86400)
    shutil.copy(src, wd / f"{vid}.src")
    (wd / f"{vid}.downloaded").write_text(str(job["size"]))
    if state == "encoded":
        secs = x265_encode(wd / f"{vid}.src", wd / f"{vid}.out.mkv", None,
                           wd / f"{vid}.log", threading.Event())
        (wd / f"{vid}.encoded").write_text(str(secs))
    return job


def stop_on_claim(pc, monkeypatch):
    """The first claim stops the worker (Stop after) and hands out nothing."""
    claims = []

    def claim(*a, **k):
        claims.append(k)
        pc.stop(now=False)
        return {"job": None, "retry_after": 0.01}

    monkeypatch.setattr(pc.client, "claim", claim)
    return claims


def test_restart_resumes_downloaded_job_without_redownload(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    downloads = []
    orig_download = pc.download_rclone.download
    monkeypatch.setattr(pc.download_rclone, "download", lambda path, local, cancel=None: (
        downloads.append(path), orig_download(path, local, cancel=cancel))[1])
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert downloads == []
        assert wait_for(lambda: pc.done == 1)
    finally:
        pc.stop(now=True)
        t.join(15)


def test_restart_resumes_encoded_job_straight_to_upload(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "encoded")

    def must_not_encode(*a, **k):
        raise AssertionError("an encoded job must not be encoded again")

    pc.encode = must_not_encode
    monkeypatch.setattr(pc.download_rclone, "download", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an encoded job must not be downloaded again")))
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert wait_for(lambda: pc.done == 1)
    finally:
        pc.stop(now=True)
        t.join(15)


def test_resume_unreachable_keeps_local_files(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    files = sorted(pc.cfg.workdir.iterdir())
    calls = []
    orig_resume = pc.client.resume

    def flaky(worker, encoder, download_account, jobs):
        calls.append(jobs)
        assert sorted(pc.cfg.workdir.iterdir()) == files  # nothing deleted meanwhile
        if len(calls) <= 2:
            raise JobServiceError("job service down")
        return orig_resume(worker, encoder, download_account, jobs)

    monkeypatch.setattr(pc.client, "resume", flaky)
    claims = []
    orig_claim = pc.client.claim
    monkeypatch.setattr(pc.client, "claim", lambda *a, **k: (
        claims.append(len(calls)), orig_claim(*a, **k))[1])
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert len(calls) == 3
        assert calls[0] == [{"id": vid, "size": db.get(conn, vid)["size"],
                             "modtime": db.get(conn, vid)["modtime"],
                             "reserve_secs": 7 * 86400}]
        assert claims and min(claims) == 3  # the downloader waited for the answer
    finally:
        pc.stop(now=True)
        t.join(15)


def test_resume_unreachable_then_stop_deletes_nothing(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    files = sorted(pc.cfg.workdir.iterdir())

    def down(*a, **k):
        pc.stop(now=True)
        raise JobServiceError("job service down")

    monkeypatch.setattr(pc.client, "resume", down)
    claims = stop_on_claim(pc, monkeypatch)
    t = run_in_thread(pc)
    t.join(15)
    assert not t.is_alive()
    assert claims == []
    assert sorted(pc.cfg.workdir.iterdir()) == files
    assert_reserved(conn, vid)


def test_resume_malformed_reply_keeps_files_and_retries(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    files = sorted(pc.cfg.workdir.iterdir())
    calls = []
    orig_resume = pc.client.resume

    def malformed(worker, encoder, download_account, jobs):
        calls.append(1)
        assert sorted(pc.cfg.workdir.iterdir()) == files  # nothing deleted meanwhile
        if len(calls) == 1:
            return {}  # an empty 200 body
        if len(calls) == 2:
            return {"resumed": []}  # no "refused" list
        return orig_resume(worker, encoder, download_account, jobs)

    monkeypatch.setattr(pc.client, "resume", malformed)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert len(calls) == 3
    finally:
        pc.stop(now=True)
        t.join(15)


def test_resume_unlisted_id_kept(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    files = sorted(pc.cfg.workdir.iterdir())
    logs = []
    pc.log = logs.append
    monkeypatch.setattr(pc.client, "resume", lambda *a: {"resumed": [], "refused": []})
    stop_on_claim(pc, monkeypatch)
    t = run_in_thread(pc)
    t.join(15)
    assert not t.is_alive()
    assert sorted(pc.cfg.workdir.iterdir()) == files  # kept, not cleaned
    assert vid not in pc.queue.held_ids()  # but not worked on
    assert any("neither resumed nor refused" in m for m in logs)
    assert_reserved(conn, vid)


def test_refused_resume_deletes_local_files(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    db.mark(conn, vid, "leased", worker="other", encoder="nvenc")  # someone else has it
    conn.commit()
    released = []
    monkeypatch.setattr(pc.client, "release", lambda job_id, worker: released.append(job_id))
    stop_on_claim(pc, monkeypatch)
    t = run_in_thread(pc)
    t.join(15)
    assert not t.is_alive()
    assert not list(pc.cfg.workdir.glob(f"{vid}.*"))
    assert released == []
    assert db.get(conn, vid)["worker"] == "other"


def test_partial_download_released_on_start(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "partial")
    stray = pc.cfg.workdir / "999999.src"
    stray.write_bytes(b"a job that no longer exists")
    resumes = []
    monkeypatch.setattr(pc.client, "resume", lambda *a, **k: resumes.append(a))
    stop_on_claim(pc, monkeypatch)
    t = run_in_thread(pc)
    t.join(15)
    assert not t.is_alive()
    assert resumes == []  # nothing left to resume
    row = db.get(conn, vid)
    assert (row["stage"], row["worker"], row["attempts"]) == ("queued", None, 0)
    assert not list(pc.cfg.workdir.glob(f"{vid}.*")) and not stray.exists()


def test_claims_never_send_resume(conn, setup, monkeypatch):
    root, pc, store = setup
    resumes = []
    orig_claim = pc.client.claim

    def spy_claim(worker, encoder, download_account="main", resume=False, **filters):
        resumes.append(resume)
        return orig_claim(worker, encoder, download_account=download_account, resume=resume,
                          **filters)

    monkeypatch.setattr(pc.client, "claim", spy_claim)
    t = run_in_thread(pc)
    assert wait_for(lambda: len(resumes) >= 2)
    pc.stop(now=True)
    t.join(10)
    assert not any(resumes)


def test_resume_full_size_src_without_marker_is_downloaded_again(conn, cfg, setup, monkeypatch):
    """rclone's local backend preallocates the full size, so a full-size src left
    over from a shutdown mid-download can't be trusted without the .downloaded
    marker proving the transfer actually completed."""
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    src = pc.cfg.workdir / f"{vid}.src"
    shutil.copy(root / "Movies" / "a.mkv", src)
    # No .downloaded marker.
    orig_download = pc.download_rclone.download
    download_calls = []

    def spy_download(*a, **k):
        download_calls.append(1)
        return orig_download(*a, **k)

    monkeypatch.setattr(pc.download_rclone, "download", spy_download)
    t = run_in_thread(pc)
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    pc.stop(now=True)
    t.join(10)
    assert download_calls  # re-downloaded, not trusted


def test_resume_stale_out_without_marker_is_regenerated(conn, setup):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    out = pc.cfg.workdir / f"{vid}.out.mkv"
    out.write_bytes(b"not a real mkv, left over from a crash")
    orig_encode = pc.encode
    calls = []

    def spy_encode(*a, **k):
        calls.append(1)
        return orig_encode(*a, **k)

    pc.encode = spy_encode
    t = run_in_thread(pc)
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
    pc.stop(now=True)
    t.join(10)
    assert calls  # the encode actually ran -- out was regenerated, not trusted stale


def test_resume_cleans_stale_files_of_another_job_at_start(conn, setup):
    root, pc, store = setup
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    stale = pc.cfg.workdir / "999999.src"
    stale.write_bytes(b"leftover from a job that no longer exists")
    t = run_in_thread(pc)
    assert wait_for(lambda: not stale.exists())
    pc.stop(now=True)
    t.join(10)


# --- cloud worker settings ---------------------------------------------------

def _probe():
    from vidshrink.probe import Probe
    return Probe("matroska", 60.0, "h264", 1920, 1080, 0, 8e6, False, None, None, None,
                 None, None, [["0", "video", "h264"]])


def _one_claim_pc(tmp_path, monkeypatch, **cfg):
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    pc = None

    def claim():
        pc.stop(now=False)
        return {"job": None, "retry_after": 0.01}

    pc = fake_pc(tmp_path, client=FakeClient(claim=claim, heartbeat=lambda: {"cancel": False}),
                 heartbeat_secs=0.02, **cfg)
    return pc


def test_cloud_config_claims_as_x265_cloud(tmp_path, monkeypatch):
    pc = _one_claim_pc(tmp_path, monkeypatch, encoder="x265-cloud", download_account="cloud",
                       max_hours=24.0, disk_check=True)
    pc.run()
    c = pc.client.claims[0]
    assert (c["encoder"], c["download_account"], c["max_hours"]) == ("x265-cloud", "cloud", 24.0)
    assert isinstance(c["free_bytes"], int) and c["free_bytes"] > 0


def test_pc_defaults_send_no_new_fields(tmp_path, monkeypatch):
    pc = _one_claim_pc(tmp_path, monkeypatch)
    pc.run()
    c = pc.client.claims[0]
    assert (c["encoder"], c["download_account"]) == ("nvenc", "main")
    assert c["free_bytes"] is None and c["max_hours"] is None


def test_x265_encoder_uses_server_command(tmp_path, monkeypatch):
    from vidshrink import pc_worker
    seen = {}

    def fake_run(cmd, log_path, cancel):
        seen["cmd"] = cmd
        return 1.5

    def no_nvenc(*a, **k):
        raise AssertionError("nvenc command must not be built")

    monkeypatch.setattr(pc_worker, "run_encode_cancellable", fake_run)
    monkeypatch.setattr(pc_worker, "build_nvenc_command", no_nvenc)
    pcfg = PCConfig(server="http://x", token="t", workdir=tmp_path / "w", worker_name="c",
                    encoder="x265-cloud")
    pc = PCWorker(pcfg, FakeClient(), FakeRemote())
    secs = pc.encode(Path("in.mkv"), Path("out.mkv"), _probe(), tmp_path / "l", threading.Event())
    assert secs == 1.5
    assert "libx265" in seen["cmd"]


class _DeadLogin(FakeRemote):
    def download(self, path, dest, cancel=None):
        raise RcloneError(1, "couldn't fetch token: invalid_grant: maybe token expired?")


def _auth_failure_pc(tmp_path, monkeypatch):
    job = dict(JOB, size=10, probe=_probe().__dict__)
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    client = FakeClient(claim=lambda: {"job": job}, heartbeat=lambda: {"cancel": False})
    return fake_pc(tmp_path, client=client, remote=_DeadLogin(), heartbeat_secs=0.02)


def test_auth_failure_sends_alert_and_stops(tmp_path, monkeypatch):
    pc = _auth_failure_pc(tmp_path, monkeypatch)
    pc.run()
    client = pc.client
    assert pc.state == "stopped"
    assert client.released == [7]
    assert client.reports == []
    assert len(client.alerts) == 1 and client.alerts[0][0] == "pc"
    assert "re-authorize" in client.alerts[0][1]
    assert len(client.claims) == 1


def test_auth_failure_alert_is_best_effort(tmp_path, monkeypatch):
    pc = _auth_failure_pc(tmp_path, monkeypatch)

    def bad_alert(worker, message):
        raise JobServiceError("down")

    pc.client.alert = bad_alert
    pc.run()
    assert pc.client.released == [7] and pc.client.reports == []


class _FullDisk(FakeRemote):
    def download(self, path, dest, cancel=None):
        raise OSError(28, "No space left on device")


def test_local_fault_sends_alert_and_stops(tmp_path, monkeypatch):
    job = dict(JOB, size=10, probe=_probe().__dict__)
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    client = FakeClient(claim=lambda: {"job": job}, heartbeat=lambda: {"cancel": False})
    pc = fake_pc(tmp_path, client=client, remote=_FullDisk(), heartbeat_secs=0.02)
    pc.run()
    assert client.released == [7] and client.reports == []
    assert len(client.alerts) == 1 and client.alerts[0][0] == "pc"
    assert "No space left" in client.alerts[0][1]
    assert len(client.claims) == 1


def test_auth_alert_names_the_download_remote(tmp_path, monkeypatch):
    job = dict(JOB, size=10, probe=_probe().__dict__)
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    client = FakeClient(claim=lambda: {"job": job}, heartbeat=lambda: {"cancel": False})
    dead = _DeadLogin()
    dead.remote = "gdrive-cloud:"
    pc = fake_pc(tmp_path, client=client, remote=FakeRemote(), heartbeat_secs=0.02)
    pc.download_rclone = dead
    pc.run()
    assert len(client.alerts) == 1
    assert client.alerts[0][1].startswith("Drive login failed for gdrive-cloud:;")


def test_empty_bwlimit_adds_no_flag():
    from vidshrink.pc_worker import pc_download_rclone, pc_rclone
    for make in (pc_rclone, pc_download_rclone):
        assert "--bwlimit" not in make(PCConfig(server="x", token="y", bwlimit="")).extra_args
        a = make(PCConfig(server="x", token="y")).extra_args
        assert a[a.index("--bwlimit") + 1] == "6M:off"
        a = make(PCConfig(server="x", token="y", bwlimit="6M:40M")).extra_args
        assert a[a.index("--bwlimit") + 1] == "6M:40M"


def test_resume_unexpected_error_retries(conn, setup, monkeypatch):
    # Anything resume() raises -- not only JobServiceError -- is retried every
    # idle_secs with nothing deleted; run() must not end (the cloud VM's unit
    # wouldn't restart a clean exit).
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_job(conn, store, pc, root, vid, "downloaded")
    files = sorted(pc.cfg.workdir.iterdir())
    calls = []
    orig_resume = pc.client.resume
    bad = [LeaseLost("409 from a confused proxy"), ValueError("non-JSON 200 body"),
           TypeError("odd"), {"resumed": [{"id": "garbage"}], "refused": []},
           {"resumed": ["not a dict"], "refused": []}, {"resumed": [], "refused": [None]}]

    def flaky(worker, encoder, download_account, jobs):
        calls.append(1)
        assert sorted(pc.cfg.workdir.iterdir()) == files  # nothing deleted meanwhile
        if len(calls) <= len(bad):
            reply = bad[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return orig_resume(worker, encoder, download_account, jobs)

    monkeypatch.setattr(pc.client, "resume", flaky)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert len(calls) == len(bad) + 1
        assert t.is_alive()
    finally:
        pc.stop(now=True)
        t.join(15)


def test_src_deleted_after_encode(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    seen = []
    orig_upload = pc.rclone.upload

    def spy(local, path, cancel=None):
        wd = pc.cfg.workdir
        seen.append(((wd / f"{vid}.src").exists(), (wd / f"{vid}.encoded").exists()))
        return orig_upload(local, path, cancel=cancel)

    monkeypatch.setattr(pc.rclone, "upload", spy)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert seen == [(False, True)]  # the source went once the encode was done
    finally:
        pc.stop(now=True)
        t.join(15)


def test_uploaded_report_failure_keeps_job_and_retries(conn, setup, monkeypatch):
    # The server is down for longer than _report_uploaded's own retries: the job is
    # kept (marker written, still held and heartbeated) and only the report is
    # retried later -- no second encode or upload.
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    wd = pc.cfg.workdir
    orig_report, orig_upload, orig_encode = pc.client.report, pc.rclone.upload, pc.encode
    calls, uploads, encodes, while_down = [], [], [], []
    gate = threading.Event()

    def flaky(job_id, worker, outcome, **data):
        if outcome != "uploaded":
            return orig_report(job_id, worker, outcome, **data)
        calls.append(data)
        if len(calls) <= pc.cfg.retry_attempts + 2:
            if len(calls) > pc.cfg.retry_attempts:
                while_down.append((json.loads((wd / f"{vid}.uploaded").read_text()),
                                   vid in pc.queue.held_ids()))
                if len(calls) == pc.cfg.retry_attempts + 1:
                    gate.wait(30)  # the test checks the server's row meanwhile
            raise JobServiceError("job service down", status=503)
        return orig_report(job_id, worker, outcome, **data)

    monkeypatch.setattr(pc.client, "report", flaky)
    monkeypatch.setattr(pc.rclone, "upload", lambda local, path, cancel=None: (
        uploads.append(path), orig_upload(local, path, cancel=cancel))[1])
    pc.encode = lambda *a: (encodes.append(1), orig_encode(*a))[1]
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: len(while_down) == 1)
        # Kept reserved, heartbeated, while the report is owed.
        lease = db.get(conn, vid)["lease_until"]
        assert wait_for(lambda: db.get(conn, vid)["lease_until"] > lease, timeout=10)
        row = db.get(conn, vid)
        assert (row["stage"], row["worker"], row["reserve_secs"]) == ("leased", "pc", 7 * 86400)
        gate.set()
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        assert wait_for(lambda: not list(wd.glob(f"{vid}.*")))
        row = db.get(conn, vid)
        tmp = root / "Movies" / "a.vidshrink-tmp.mkv"
        assert tmp.exists() and md5_file(tmp) == row["out_md5"]
        assert len(calls) == pc.cfg.retry_attempts + 3
        assert len(while_down) == 2
        for marker, held in while_down:
            assert marker == {"out_size": row["out_size"], "md5": row["out_md5"],
                              "encode_secs": pytest.approx(row["encode_secs"])}
            assert held
        assert len(uploads) == 1 and len(encodes) == 1
        assert pc.done == 1
    finally:
        gate.set()
        pc.stop(now=True)
        t.join(15)


def leave_uploaded(conn, store, pc, root, vid, tmp_on_drive=True):
    """A job a previous run uploaded but couldn't report: encoded, source deleted,
    `.uploaded` marker written and (unless tmp_on_drive=False) its tmp on Drive."""
    job = leave_job(conn, store, pc, root, vid, "encoded")
    wd = pc.cfg.workdir
    out = wd / f"{vid}.out.mkv"
    (wd / f"{vid}.src").unlink()
    if tmp_on_drive:
        shutil.copy(out, root / job["tmp_path"])
    (wd / f"{vid}.uploaded").write_text(json.dumps(
        {"out_size": out.stat().st_size, "md5": md5_file(out), "encode_secs": 1.5}))
    return job


def test_restart_retries_pending_uploaded_report(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    job = leave_uploaded(conn, store, pc, root, vid)
    tmp = root / job["tmp_path"]
    tmp_md5 = md5_file(tmp)

    def must_not(*a, **k):
        raise AssertionError("a pending report must not download, encode or upload")

    pc.encode = must_not
    monkeypatch.setattr(pc.download_rclone, "download", must_not)
    monkeypatch.setattr(pc.rclone, "upload", must_not)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        row = db.get(conn, vid)
        assert (row["out_md5"], row["encode_secs"]) == (tmp_md5, 1.5)
        assert tmp.exists() and md5_file(tmp) == tmp_md5
        assert wait_for(lambda: pc.done == 1)
        assert wait_for(lambda: not list(pc.cfg.workdir.glob(f"{vid}.*")))
    finally:
        pc.stop(now=True)
        t.join(15)


def test_restart_pending_report_uploads_again_when_tmp_gone(conn, setup, monkeypatch):
    # The reservation ran out while switched off: the server deleted the tmp and
    # requeued the row, and /resume leased it back. Only then is it uploaded again.
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    leave_uploaded(conn, store, pc, root, vid, tmp_on_drive=False)
    store.release(vid, "pc")  # as expire_leases leaves it

    def must_not(*a, **k):
        raise AssertionError("must not download or encode again")

    pc.encode = must_not
    monkeypatch.setattr(pc.download_rclone, "download", must_not)
    t = run_in_thread(pc)
    try:
        assert wait_for(lambda: db.get(conn, vid)["stage"] == "remote_ready")
        tmp = root / "Movies" / "a.vidshrink-tmp.mkv"
        assert tmp.exists() and md5_file(tmp) == db.get(conn, vid)["out_md5"]
    finally:
        pc.stop(now=True)
        t.join(15)


def test_stop_now_during_report_retries_keeps_job_for_next_start(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    orig_report = pc.client.report

    def down(job_id, worker, outcome, **data):
        if outcome != "uploaded":
            return orig_report(job_id, worker, outcome, **data)
        pc.stop(now=True)
        raise JobServiceError("job service down", status=None)

    monkeypatch.setattr(pc.client, "report", down)
    pc.cfg.retry_base_delay = 5.0  # so the Stop lands during the backoff
    t = run_in_thread(pc)
    t.join(60)
    assert not t.is_alive()
    wd = pc.cfg.workdir
    for ext in (".job.json", ".out.mkv", ".uploaded"):
        assert (wd / f"{vid}{ext}").exists(), ext
    assert_reserved(conn, vid)
    assert (root / "Movies" / "a.vidshrink-tmp.mkv").exists()
