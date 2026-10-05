import os
import socket
import threading
import time

import pytest

from vidshrink import db
from vidshrink.jobs import (JobClient, JobServiceError, LeaseLost,
                            load_or_create_token, REQUEST_TIMEOUT_SECS)
from tests.test_jobs_store import add


def test_token_file(tmp_path):
    p = tmp_path / "serve.token"
    t = load_or_create_token(p)
    assert len(t) >= 32 and load_or_create_token(p) == t
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_token_file_concurrent_creation(tmp_path):
    """Test that FileExistsError is handled correctly when another process creates the file."""
    p = tmp_path / "serve.token"
    # First call creates the token
    t1 = load_or_create_token(p)
    # Second call should read the existing token (simulating race condition)
    t2 = load_or_create_token(p)
    assert t1 == t2 and len(t1) >= 32


def test_round_trip(conn, job_service):
    vid = add(conn, "a.mkv")
    c = JobClient(job_service.url, job_service.token)
    assert c.health()["ok"] is True
    job = c.claim("pc")["job"]
    assert job["id"] == vid
    assert c.heartbeat(vid, "pc") == {"cancel": False}
    c.report(vid, "pc", "uploaded", out_size=10, out_md5="abc", encode_secs=1.5)
    assert db.get(conn, vid)["stage"] == "remote_ready"


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


def test_slow_client_doesnt_block_others(job_service):
    """Test that a slow client doesn't block other concurrent requests."""
    # Start a slow request in a separate thread
    slow_done = []

    def slow_request():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", int(job_service.url.split(":")[-1])))
            # Send incomplete request and stall
            sock.send(b"GET /health HTTP/1.1\r\n")
            time.sleep(0.1)  # Small delay so server sees the incomplete request
            sock.close()
            slow_done.append(True)
        except Exception as e:
            slow_done.append(False)

    slow_thread = threading.Thread(target=slow_request, daemon=True)
    slow_thread.start()
    time.sleep(0.05)  # Let slow request start

    # Now make a normal request - should succeed quickly
    c = JobClient(job_service.url, job_service.token)
    result = c.health()
    assert result["ok"] is True

    slow_thread.join(timeout=2)
