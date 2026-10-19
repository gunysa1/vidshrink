import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.encode import run_encode_cancellable
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
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: calls["n"] >= 1)
    assert wait_for(lambda: released == [vid])  # the release actually happened
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "queued"
                    and db.get(conn, vid)["attempts"] == 0)
    assert wait_for(lambda: calls["n"] >= 2)  # the loop survived and claimed it again
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


def test_stop_now_during_upload_releases_and_cleans_tmp(conn, setup, monkeypatch):
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
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)
    assert not tmp_on_remote.exists()


def test_rate_limited_upload_is_reported_upload_limited(conn, setup, monkeypatch):
    root, pc, store = setup

    def limited(local, path, cancel=None):
        raise RcloneError(1, "googleapi: Error 403: User rate limit exceeded.,"
                          " userRateLimitExceeded")

    monkeypatch.setattr(pc.rclone, "upload", limited)
    t = run_in_thread(pc)
    assert wait_for(lambda: store.uploads.blocked())
    pc.stop(now=True)
    t.join(10)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert (db.get(conn, vid)["stage"], db.get(conn, vid)["attempts"]) == ("queued", 0)


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

    def flaky(job_id, worker, outcome, **data):
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


def test_poison_job_reported_failed_on_second_unexpected_error(conn, setup):
    root, pc, store = setup
    calls = {"n": 0}

    def boom(src, out, probe, log_path, cancel):
        calls["n"] += 1
        raise TypeError("always broken")

    pc.encode = boom
    t = run_in_thread(pc)
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    assert wait_for(lambda: calls["n"] >= 1)
    assert wait_for(lambda: db.get(conn, vid)["stage"] == "queued"
                    and db.get(conn, vid)["attempts"] == 0)  # first: released
    assert wait_for(lambda: calls["n"] >= 2)
    assert wait_for(lambda: db.get(conn, vid)["attempts"] == 1)  # second: reported failed
    assert db.get(conn, vid)["stage"] == "queued"  # max_attempts=2, so still queued
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

    def heartbeat(self, job_id, worker, download_account="main"):
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


def test_heartbeat_outage_longer_than_lease_cancels_as_server_cancel(tmp_path):
    def down():
        raise JobServiceError("cannot reach job service")

    pc = fake_pc(tmp_path, client=FakeClient(heartbeat=down), heartbeat_secs=0.02,
                 lease_secs=0.3)
    stop, cancel, server_cancelled = threading.Event(), threading.Event(), threading.Event()
    t = threading.Thread(target=pc._heartbeat, args=(7, stop, cancel, server_cancelled),
                         daemon=True)
    start = time.monotonic()
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert cancel.is_set() and server_cancelled.is_set()
    assert time.monotonic() - start >= 0.3


def test_heartbeat_outage_resets_on_success(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 3:  # fail twice, succeed once, ...
            raise JobServiceError("blip")
        return {"cancel": False}

    pc = fake_pc(tmp_path, client=FakeClient(heartbeat=flaky), heartbeat_secs=0.02,
                 lease_secs=0.15)
    stop, cancel, server_cancelled = threading.Event(), threading.Event(), threading.Event()
    t = threading.Thread(target=pc._heartbeat, args=(7, stop, cancel, server_cancelled),
                         daemon=True)
    t.start()
    time.sleep(0.6)
    stop.set()
    t.join(5)
    assert not cancel.is_set() and not server_cancelled.is_set()


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
        claim=claim, heartbeat=lambda: {"cancel": False, "download_used": 13e9,
                                        "download_limit": 500e9}), heartbeat_secs=0.02)
    assert pc.download_today is None
    pc.run()
    assert pc.download_today == (12e9, 500e9)
    stop = threading.Event()
    t = threading.Thread(target=pc._heartbeat,
                         args=(7, stop, threading.Event(), threading.Event()), daemon=True)
    t.start()
    assert wait_for(lambda: pc.download_today == (13e9, 500e9), timeout=5)
    stop.set()
    t.join(5)


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

def test_resume_reuses_finished_encode_skips_download_and_encode(conn, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    out = pc.cfg.workdir / f"{vid}.out.mkv"
    marker = pc.cfg.workdir / f"{vid}.encoded"
    x265_encode(root / "Movies" / "a.mkv", out, None, pc.cfg.workdir / "setup.log",
               threading.Event())
    marker.write_text("12.5")

    def must_not_encode(*a, **k):
        raise AssertionError("encode must not be called when reusing a finished encode")

    pc.encode = must_not_encode
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
    assert download_calls == []


def test_resume_reuses_downloaded_src_skips_download(conn, cfg, setup, monkeypatch):
    root, pc, store = setup
    vid = db.get_by_path(conn, "Movies/a.mkv")["id"]
    pc.cfg.workdir.mkdir(parents=True, exist_ok=True)
    src = pc.cfg.workdir / f"{vid}.src"
    shutil.copy(root / "Movies" / "a.mkv", src)
    (pc.cfg.workdir / f"{vid}.downloaded").write_text(str(src.stat().st_size))
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
    assert download_calls == []


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


def test_first_claim_after_start_sends_resume_true_then_false(conn, setup, monkeypatch):
    root, pc, store = setup
    orig_claim = pc.client.claim
    resumes = []

    def spy_claim(worker, encoder, download_account="main", resume=False):
        resumes.append(resume)
        return orig_claim(worker, encoder, download_account=download_account, resume=resume)

    monkeypatch.setattr(pc.client, "claim", spy_claim)
    t = run_in_thread(pc)
    assert wait_for(lambda: len(resumes) >= 2)
    pc.stop(now=True)
    t.join(10)
    assert resumes[0] is True
    assert all(r is False for r in resumes[1:])


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


def test_failed_first_claim_keeps_resume_for_the_next(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, **k: "/bin/" + name)
    calls = []
    pc = None

    def claim():
        calls.append(1)
        if len(calls) == 1:
            raise JobServiceError("tailscale not up yet")
        pc.stop(now=False)
        return {"job": None, "retry_after": 0.01}

    pc = fake_pc(tmp_path, client=FakeClient(claim=claim), idle_secs=0.01)
    pc.run()
    assert [c["resume"] for c in pc.client.claims] == [True, True]
    assert len(calls) == 2


def test_empty_bwlimit_adds_no_flag():
    from vidshrink.pc_worker import pc_download_rclone, pc_rclone
    for make in (pc_rclone, pc_download_rclone):
        assert "--bwlimit" not in make(PCConfig(server="x", token="y", bwlimit="")).extra_args
        a = make(PCConfig(server="x", token="y")).extra_args
        assert a[a.index("--bwlimit") + 1] == "6M:off"
        a = make(PCConfig(server="x", token="y", bwlimit="6M:40M")).extra_args
        assert a[a.index("--bwlimit") + 1] == "6M:40M"
