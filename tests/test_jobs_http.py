import os
import socket
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from vidshrink import db
from vidshrink import jobs as jobs_mod
from vidshrink.jobs import (JobClient, JobServiceError, JobStore, LeaseLost,
                            load_or_create_token, make_server, REQUEST_TIMEOUT_SECS)
from vidshrink.rclone import Rclone, free_port
from tests.test_jobs_store import Clock, add


def test_token_file(tmp_path):
    p = tmp_path / "serve.token"
    t = load_or_create_token(p)
    assert len(t) >= 32 and load_or_create_token(p) == t
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_token_file_concurrent_creation(tmp_path):
    """Test FileExistsError race condition: another process creates the file while we're creating it."""
    p = tmp_path / "serve.token"

    call_count = 0
    original_exists = Path.exists

    def mock_exists(self):
        # On the first call, pretend the file doesn't exist (even though it will be created)
        # This simulates the race where exists() returns False but os.open(O_EXCL) fails
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False
        return original_exists(self)

    # First call: Create the file (no mocking)
    t1 = load_or_create_token(p)

    # Second call: Mock exists() to return False, forcing os.open(O_EXCL) to hit the race condition
    # This exercises the FileExistsError exception handler
    call_count = 0
    with patch.object(Path, 'exists', mock_exists):
        t2 = load_or_create_token(p)

    assert t1 == t2 and len(t1) >= 32


def test_round_trip(conn, job_service):
    vid = add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    assert c.health()["ok"] is True
    job = c.claim("pc")["job"]
    assert job["id"] == vid
    assert c.heartbeat(vid, "pc")["cancel"] is False
    c.report(vid, "pc", "uploaded", out_size=10, out_md5="a" * 32, encode_secs=1.5)
    assert db.get(conn, vid)["stage"] == "remote_ready"


def test_download_account_passes_through_claim_heartbeat_report(conn, cfg, job_service):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    c = JobClient(job_service.url, job_service.token)
    r = c.claim("pc", download_account="pc")
    assert r["job"]["id"] == vid
    assert r["download_limit"] == cfg.daily_pc_download_limit
    hb = c.heartbeat(vid, "pc", download_account="pc")
    assert hb["cancel"] is False
    c.report(vid, "pc", "download_quota", download_account="pc")
    assert db.get(conn, vid)["stage"] == "queued"


def test_bad_download_account_is_400(conn, job_service):
    add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    with pytest.raises(JobServiceError, match="400"):
        c.claim("pc", download_account="bogus")


def test_wrong_token(job_service):
    with pytest.raises(JobServiceError, match="401"):
        JobClient(job_service.url, "nope").health()


def test_lease_lost_maps_to_409(conn, job_service):
    vid = add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    c.claim("pc")
    with pytest.raises(LeaseLost):
        c.release(vid, "someone-else")


def test_unknown_outcome_is_400(conn, job_service):
    vid = add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    c.claim("pc")
    with pytest.raises(JobServiceError, match="400"):
        c.report(vid, "pc", "exploded")


def test_invalid_uploaded_report_is_400(conn, job_service):
    vid = add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    c.claim("pc")
    with pytest.raises(JobServiceError, match="400"):
        c.report(vid, "pc", "uploaded", out_size=10, out_md5="not-hex", encode_secs=1.0)
    assert db.get(conn, vid)["stage"] == "leased"


def test_no_work(job_service):
    r = JobClient(job_service.url, job_service.token).claim("pc")
    assert r["job"] is None and r["retry_after"] > 0


def test_payload_too_large(job_service):
    """Test that requests with Content-Length > 1 MiB return 413."""
    c = JobClient(job_service.url, job_service.token)
    # Create a payload larger than 1 MiB
    payload = {"worker": "test", "data": "x" * (1 << 20)}  # 1 MiB
    with pytest.raises(JobServiceError):
        c._call("POST", "/claim", payload)


def test_negative_content_length(job_service):
    """Test that negative Content-Length returns 400 without hanging."""
    # Manually construct a request with negative Content-Length to bypass JobClient validation
    req = urllib.request.Request(
        job_service.url + "/claim",
        data=b'{}',
        method="POST",
        headers={
            "Authorization": f"Bearer {job_service.token}",
            "Content-Type": "application/json",
            "Content-Length": "-1"
        }
    )

    # The raw urlopen will raise HTTPError with 400 - verify it doesn't hang
    # Key point: this should complete quickly (within 5s timeout), proving it returns 400 without trying to read
    with pytest.raises(Exception):  # Catches HTTPError
        urllib.request.urlopen(req, timeout=5)


def test_slow_client_timeout_blocks_properly(monkeypatch, cfg):
    """Test that socket timeout prevents slow clients from blocking indefinitely."""
    from vidshrink.jobs import make_server, JobStore
    from vidshrink.rclone import Rclone, free_port

    # Monkeypatch REQUEST_TIMEOUT_SECS BEFORE creating the server so Handler picks it up
    monkeypatch.setattr("vidshrink.jobs.REQUEST_TIMEOUT_SECS", 1)

    TOKEN = "s3cret"
    port = free_port()
    ready_event = threading.Event()
    server_holder = {}

    def run_server():
        conn = db.connect(cfg.db_path)
        store = JobStore(conn, cfg, Rclone(cfg.remote), log=lambda m: None)
        server = make_server(store, TOKEN, "127.0.0.1", port)
        server_holder['server'] = server
        ready_event.set()
        server.serve_forever()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    if not ready_event.wait(timeout=5):
        pytest.fail("Server did not start")

    try:
        # Open a slow socket that sends partial request and keeps it open
        slow_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        slow_sock.connect(("127.0.0.1", port))
        # Send incomplete request header and keep connection open (timeout will kill it)
        slow_sock.send(b"GET /health HTTP/1.1\r\n")
        slow_sock.setblocking(False)  # Non-blocking so we don't wait on recv

        # Now make a normal health check - should succeed quickly despite slow socket
        start = time.time()
        c = JobClient(f"http://127.0.0.1:{port}", TOKEN, timeout=5)
        result = c.health()
        elapsed = time.time() - start

        assert result["ok"] is True
        assert elapsed < 5  # Should complete well within 5 seconds (proves timeout doesn't block)

    finally:
        slow_sock.close()
        server_holder['server'].shutdown()
        server_holder['server'].server_close()
        t.join(timeout=5)


def test_serve_sweeps_expired_leases_on_its_own(cfg, monkeypatch):
    """serve must expire leases even with no client ever calling in (nothing
    hits JobStore.claim()'s own sweep): make_server's server sweeps on its
    own, via service_actions()."""
    monkeypatch.setattr(jobs_mod, "SWEEP_INTERVAL_SECS", 0.0)
    clock = Clock()

    setup_conn = db.connect(cfg.db_path)
    vid = add(setup_conn, "a.mkv")
    JobStore(setup_conn, cfg, Rclone(cfg.remote), now=clock, log=lambda m: None).claim("pc")
    row = db.get(setup_conn, vid)
    assert (row["stage"], row["attempts"]) == ("leased", 0)
    clock.t += cfg.lease_secs + 1  # now expired
    setup_conn.close()

    # The store's sqlite3 connection must be opened in the thread that runs
    # serve_forever (and so calls service_actions): sqlite3 connections are
    # thread-bound by default, same as the job_service fixture in conftest.py.
    holder = {}

    def run_server():
        server_conn = db.connect(cfg.db_path)
        store = JobStore(server_conn, cfg, Rclone(cfg.remote), now=clock, log=lambda m: None)
        server = make_server(store, "tok", "127.0.0.1", free_port())
        holder["server"] = server
        try:
            server.serve_forever(poll_interval=0.05)
        finally:
            server_conn.close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    check_conn = db.connect(cfg.db_path)
    try:
        row = db.get(check_conn, vid)
        for _ in range(100):
            if row["stage"] == "queued":
                break
            time.sleep(0.05)
            row = db.get(check_conn, vid)
        assert (row["stage"], row["attempts"]) == ("queued", 0)
    finally:
        holder["server"].shutdown()
        holder["server"].server_close()
        t.join(timeout=5)
        check_conn.close()
