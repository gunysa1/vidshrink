import os
import threading

import pytest

from vidshrink import db
from vidshrink.jobs import (JobClient, JobServiceError, JobStore, LeaseLost,
                            load_or_create_token, make_server)
from vidshrink.rclone import Rclone, free_port
from tests.test_jobs_store import add


@pytest.fixture
def service(conn, cfg):
    store = JobStore(conn, cfg, Rclone(cfg.remote), log=lambda m: None)
    port = free_port()
    server = make_server(store, "s3cret", "127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_token_file(tmp_path):
    p = tmp_path / "serve.token"
    t = load_or_create_token(p)
    assert len(t) >= 32 and load_or_create_token(p) == t
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_round_trip(conn, service):
    vid = add(conn, "a.mkv")
    c = JobClient(service, "s3cret")
    assert c.health()["ok"] is True
    job = c.claim("pc")["job"]
    assert job["id"] == vid
    assert c.heartbeat(vid, "pc") == {"cancel": False}
    c.report(vid, "pc", "uploaded", out_size=10, out_md5="abc", encode_secs=1.5)
    assert db.get(conn, vid)["stage"] == "remote_ready"


def test_wrong_token(service):
    with pytest.raises(JobServiceError, match="401"):
        JobClient(service, "nope").health()


def test_lease_lost_maps_to_409(conn, service):
    vid = add(conn, "a.mkv")
    c = JobClient(service, "s3cret")
    c.claim("pc")
    with pytest.raises(LeaseLost):
        c.release(vid, "someone-else")


def test_unknown_outcome_is_400(conn, service):
    vid = add(conn, "a.mkv")
    c = JobClient(service, "s3cret")
    c.claim("pc")
    with pytest.raises(JobServiceError, match="400"):
        c.report(vid, "pc", "exploded")


def test_no_work(service):
    r = JobClient(service, "s3cret").claim("pc")
    assert r["job"] is None and r["retry_after"] > 0
