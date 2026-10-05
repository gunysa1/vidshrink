from dataclasses import replace
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.jobs import PC_SEEN_KEY, JobStore, LeaseLost
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone

BASE = Probe(container="matroska,webm", duration=3600.0, codec="h264", width=1920,
             height=1080, video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
             color_transfer=None, color_space=None, master_display=None, max_cll=None,
             streams=[])


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store(conn, cfg):
    clock = Clock()
    s = JobStore(conn, cfg, Rclone(cfg.remote), now=clock, log=lambda m: None)
    s.clock = clock
    return s


def add(conn, path, size=5_000_000_000, probe=BASE, score=1.0):
    db.upsert_listing(conn, path, size, "t")
    db.save_probe(conn, path, probe.to_json())
    vid = db.get_by_path(conn, path)["id"]
    db.mark(conn, vid, "queued", score=score)
    return vid


def test_claim_leases_best_nvenc_job(conn, store):
    add(conn, "small.mkv", size=1_000_000_000)
    best = add(conn, "big.mkv", size=30_000_000_000)
    r = store.claim("pc")
    job = r["job"]
    assert job["id"] == best and job["path"] == "big.mkv"
    assert job["tmp_path"] == "big.vidshrink-tmp.mkv" and job["final_path"] == "big.mkv"
    assert job["probe"]["codec"] == "h264"
    row = db.get(conn, best)
    assert (row["stage"], row["worker"], row["encoder"]) == ("leased", "pc", "nvenc")
    assert row["lease_until"] == store.clock.t + 1800
    assert store.downloads.used() == 30_000_000_000
    assert float(db.kv_get(conn, PC_SEEN_KEY)) == store.clock.t


def test_claim_skips_hdr(conn, store):
    add(conn, "hdr.mkv", size=50_000_000_000,
        probe=replace(BASE, color_transfer="smpte2084"))
    sdr = add(conn, "sdr.mkv")
    assert store.claim("pc")["job"]["id"] == sdr


def test_two_claims_get_different_rows(conn, store):
    add(conn, "a.mkv")
    add(conn, "b.mkv")
    ids = {store.claim("pc")["job"]["id"], store.claim("pc")["job"]["id"]}
    assert len(ids) == 2
    assert store.claim("pc")["job"] is None


def test_claim_respects_download_budget(conn, cfg, store):
    add(conn, "a.mkv", size=5_000_000_000)
    store.downloads.add(cfg.daily_download_limit)
    r = store.claim("pc")
    assert r["job"] is None and r["retry_after"] > 0
    assert db.get_by_path(conn, "a.mkv")["stage"] == "queued"


def test_claim_when_downloads_blocked(conn, store):
    add(conn, "a.mkv")
    store.downloads.block(3600)
    assert store.claim("pc")["job"] is None


def test_claim_fails_row_whose_target_exists(conn, cfg, store):
    root = Path(cfg.remote.rstrip("/"))
    (root / "b.mkv").write_bytes(b"someone else's")
    vid = add(conn, "b.mp4")
    assert store.claim("pc")["job"] is None
    row = db.get(conn, vid)
    assert row["stage"] == "failed" and "exists" in row["reason"]


def test_heartbeat_extends_and_cancels(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.clock.t += 600
    assert store.heartbeat(vid, "pc") == {"cancel": False}
    assert db.get(conn, vid)["lease_until"] == store.clock.t + 1800
    assert store.heartbeat(vid, "other") == {"cancel": True}
    db.mark(conn, vid, "queued")
    assert store.heartbeat(vid, "pc") == {"cancel": True}


def test_report_uploaded(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "uploaded", out_size=2_000_000_000, out_md5="abc", encode_secs=600.0)
    row = db.get(conn, vid)
    assert (row["stage"], row["out_size"], row["out_md5"], row["encode_secs"]) == \
        ("remote_ready", 2_000_000_000, "abc", 600.0)
    assert store.uploads.used() == 2_000_000_000


def test_report_failed_twice_marks_failed(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "failed", reason="boom")
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"], row["worker"]) == ("queued", 1, None)
    store.claim("pc")
    store.report(vid, "pc", "failed", reason="boom")
    assert db.get(conn, vid)["stage"] == "failed"


def test_report_not_worth_it_changed_and_quota(conn, store):
    a, b, c = add(conn, "a.mkv", score=3), add(conn, "b.mkv", score=2), add(conn, "c.mkv", score=1)
    for _ in range(3):
        store.claim("pc")
    store.report(a, "pc", "not_worth_it", reason="only 5% smaller")
    store.report(b, "pc", "changed")
    store.report(c, "pc", "download_quota")
    assert db.get(conn, a)["stage"] == "not_worth_it"
    rb = db.get(conn, b)
    assert (rb["stage"], rb["probe_json"]) == ("unprobed", None)
    rc = db.get(conn, c)
    assert (rc["stage"], rc["attempts"]) == ("queued", 0)
    assert store.downloads.blocked()


def test_report_from_wrong_worker(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    with pytest.raises(LeaseLost):
        store.report(vid, "other", "uploaded", out_size=1, out_md5="x", encode_secs=1.0)
    with pytest.raises(LeaseLost):
        store.release(vid, "other")


def test_release(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.release(vid, "pc")
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"], row["worker"], row["lease_until"]) == \
        ("queued", 0, None, None)


def test_expire_leases_removes_tmp_and_requeues(conn, cfg, store):
    root = Path(cfg.remote.rstrip("/"))
    vid = add(conn, "a.mkv")
    store.claim("pc")
    (root / "a.vidshrink-tmp.mkv").write_bytes(b"partial")
    assert store.expire_leases() == 0  # not yet
    store.clock.t += 1801
    assert store.expire_leases() == 1
    assert not (root / "a.vidshrink-tmp.mkv").exists()
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)
