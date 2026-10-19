import json
from dataclasses import replace
from pathlib import Path

import pytest

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.jobs import ALERT_PREFIX, NO_WORK_RETRY_SECS, PC_SEEN_KEY, JobStore, LeaseLost
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


def test_candidates_do_not_query_learned_per_row(store, conn, monkeypatch):
    for i in range(50):
        add(conn, f"Movies/m{i}.mkv", size=10_000_000_000)
    calls = []
    orig = db.learned_mean
    monkeypatch.setattr(db, "learned_mean", lambda *a, **k: calls.append(1) or orig(*a, **k))
    rows = store._candidates("nvenc")
    assert len(rows) == 50
    assert len(calls) <= len(scorer.DEFAULT_RATIO) + len(scorer.DEFAULT_SPEED)  # tables, not per row


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
    assert store.heartbeat(vid, "pc")["cancel"] is False
    assert db.get(conn, vid)["lease_until"] == store.clock.t + 1800
    assert store.heartbeat(vid, "other")["cancel"] is True
    db.mark(conn, vid, "queued")
    assert store.heartbeat(vid, "pc")["cancel"] is True


def test_resume_returns_same_leased_job_and_extends_lease(conn, store):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    first = store.claim("pc")["job"]
    before_used = store.downloads.used()
    store.clock.t += 100
    r = store.claim("pc", resume=True)
    assert r["job"]["id"] == vid == first["id"]
    assert r["resumed"] is True
    assert db.get(conn, vid)["lease_until"] == store.clock.t + 1800
    assert store.downloads.used() == before_used  # no extra download charged


def test_resume_ignores_another_workers_lease(conn, store):
    add(conn, "a.mkv")
    store.claim("pc")
    r = store.claim("other", resume=True)
    assert r.get("resumed") is not True
    assert r["job"] is None  # nothing else queued, so a normal claim finds nothing


def test_resume_ignores_an_expired_lease(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.clock.t += 1801  # lease_until now strictly in the past
    r = store.claim("pc", resume=True)
    assert r.get("resumed") is not True
    # Falls through to a normal claim: the expired lease gets swept and re-leased.
    assert r["job"]["id"] == vid


def test_resume_false_behaves_like_today(conn, store):
    best = add(conn, "a.mkv")
    other = add(conn, "b.mkv", size=1_000_000)
    store.claim("pc")  # leases `best` (bigger -> scores higher)
    r = store.claim("pc", resume=False)
    assert "resumed" not in r
    assert r["job"]["id"] == other


def test_report_uploaded(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    md5 = "a" * 32
    store.report(vid, "pc", "uploaded", out_size=2_000_000_000, out_md5=md5, encode_secs=600.0)
    row = db.get(conn, vid)
    assert (row["stage"], row["out_size"], row["out_md5"], row["encode_secs"]) == \
        ("remote_ready", 2_000_000_000, md5, 600.0)
    assert store.uploads.used() == 2_000_000_000


def test_report_uploaded_is_idempotent_on_retry(conn, store):
    """A PC that retries an ambiguous "uploaded" report (the first commit succeeded
    server-side but the response was lost) must not get LeaseLost and must not be
    charged the upload budget a second time."""
    vid = add(conn, "a.mkv")
    store.claim("pc")
    md5 = "a" * 32
    store.report(vid, "pc", "uploaded", out_size=2_000_000_000, out_md5=md5, encode_secs=600.0)
    assert store.uploads.used() == 2_000_000_000
    store.report(vid, "pc", "uploaded", out_size=2_000_000_000, out_md5=md5, encode_secs=600.0)
    assert store.uploads.used() == 2_000_000_000  # not charged twice
    row = db.get(conn, vid)
    assert (row["stage"], row["out_md5"]) == ("remote_ready", md5)


def test_report_uploaded_different_md5_after_remote_ready_raises(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "uploaded", out_size=10, out_md5="a" * 32, encode_secs=1.0)
    with pytest.raises(LeaseLost):
        store.report(vid, "pc", "uploaded", out_size=10, out_md5="b" * 32, encode_secs=1.0)


def test_report_uploaded_rejects_invalid_md5(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    with pytest.raises(ValueError):
        store.report(vid, "pc", "uploaded", out_size=10, out_md5="not-hex", encode_secs=1.0)
    assert db.get(conn, vid)["stage"] == "leased"  # untouched


def test_report_uploaded_rejects_non_positive_size(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    with pytest.raises(ValueError):
        store.report(vid, "pc", "uploaded", out_size=0, out_md5="a" * 32, encode_secs=1.0)
    assert db.get(conn, vid)["stage"] == "leased"  # untouched


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


def test_report_upload_limited_blocks_uploads_and_requeues(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "upload_limited")
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"], row["worker"]) == ("queued", 0, None)
    assert store.uploads.blocked()
    r = store.claim("pc")
    assert r["job"] is None and r["retry_after"] > 0


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


def test_heartbeat_after_expiry_cancels_without_reviving(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    original_lease_until = db.get(conn, vid)["lease_until"]
    store.clock.t += 1801
    assert store.heartbeat(vid, "pc")["cancel"] is True
    row = db.get(conn, vid)
    assert row["stage"] == "leased" and row["lease_until"] == original_lease_until


def test_report_and_release_after_expiry_raise(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.clock.t += 1801
    with pytest.raises(LeaseLost):
        store.report(vid, "pc", "uploaded", out_size=1, out_md5="x", encode_secs=1.0)
    with pytest.raises(LeaseLost):
        store.release(vid, "pc")
    row = db.get(conn, vid)
    assert row["stage"] == "leased"  # untouched by either failed call


def test_expire_leases_skips_lease_renewed_since_select(conn, cfg, store):
    root = Path(cfg.remote.rstrip("/"))
    vid = add(conn, "a.mkv")
    store.claim("pc")
    (root / "a.vidshrink-tmp.mkv").write_bytes(b"partial")
    # The time a stale SELECT snapshot would have seen this lease as already expired.
    stale_now = store.clock.t + 1801
    # A renewal happens (e.g. the PC's heartbeat) after that snapshot but before the
    # guarded requeue runs against it.
    store.clock.t += 2
    store.heartbeat(vid, "pc")
    assert store._requeue_if_still_expired(vid, stale_now) is False
    row = db.get(conn, vid)
    assert (row["stage"], row["worker"]) == ("leased", "pc")
    assert (root / "a.vidshrink-tmp.mkv").exists()


def test_claim_skips_same_stem_collision(conn, store):
    avi = add(conn, "Movies/a.avi")
    db.mark(conn, avi, "encoding")
    mp4 = add(conn, "Movies/a.mp4")
    r = store.claim("pc")
    assert r["job"] is None
    assert db.get(conn, mp4)["stage"] == "queued"
    db.mark(conn, avi, "done")
    r2 = store.claim("pc")
    assert r2["job"]["id"] == mp4


def test_claim_waits_when_upload_budget_used_up(conn, cfg, store):
    add(conn, "a.mkv")
    store.uploads.add(cfg.daily_upload_limit)
    r = store.claim("pc")
    assert r["job"] is None and r["retry_after"] > 0
    assert db.get_by_path(conn, "a.mkv")["stage"] == "queued"
    assert store.downloads.used() == 0  # nothing charged for a job not handed out


def test_claim_pc_account_charges_pc_downloads_not_main(conn, store):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    r = store.claim("pc", download_account="pc")
    assert r["job"]["id"] == vid
    assert store.pc_downloads.used() == 5_000_000_000
    assert store.downloads.used() == 0
    assert (r["download_used"], r["download_limit"]) == (5_000_000_000, store.cfg.daily_pc_download_limit)


def test_claim_pc_account_works_while_main_exhausted(conn, cfg, store):
    store.downloads.add(cfg.daily_download_limit)
    vid = add(conn, "a.mkv", size=5_000_000_000)
    r = store.claim("pc", download_account="pc")
    assert r["job"]["id"] == vid


def test_claim_pc_account_works_while_main_blocked(conn, store):
    store.downloads.block(3600)
    vid = add(conn, "a.mkv", size=5_000_000_000)
    r = store.claim("pc", download_account="pc")
    assert r["job"]["id"] == vid


def test_claim_main_account_default_is_unchanged(conn, store):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    r = store.claim("pc", download_account="main")
    assert r["job"]["id"] == vid
    assert store.downloads.used() == 5_000_000_000
    assert store.pc_downloads.used() == 0


def test_claim_bad_download_account_raises(conn, store):
    add(conn, "a.mkv")
    with pytest.raises(ValueError):
        store.claim("pc", download_account="bogus")


def test_report_download_quota_pc_blocks_only_pc_downloads(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc", download_account="pc")
    store.report(vid, "pc", "download_quota", download_account="pc")
    assert store.pc_downloads.blocked()
    assert not store.downloads.blocked()
    row = db.get(conn, vid)
    assert (row["stage"], row["attempts"]) == ("queued", 0)


def test_report_download_quota_main_blocks_only_main_downloads(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "download_quota")
    assert store.downloads.blocked()
    assert not store.pc_downloads.blocked()


def test_report_download_account_ignored_on_other_outcomes(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    store.report(vid, "pc", "not_worth_it", reason="x", download_account="bogus")
    assert db.get(conn, vid)["stage"] == "not_worth_it"


def test_report_bad_download_account_on_quota_raises(conn, store):
    vid = add(conn, "a.mkv")
    store.claim("pc")
    with pytest.raises(ValueError):
        store.report(vid, "pc", "download_quota", download_account="bogus")


def test_heartbeat_reports_pc_budget_when_selected(conn, store):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    store.claim("pc", download_account="pc")
    store.pc_downloads.add(1_000)
    hb = store.heartbeat(vid, "pc", download_account="pc")
    assert (hb["download_used"], hb["download_limit"]) == (
        5_000_001_000, store.cfg.daily_pc_download_limit)


def test_claim_and_heartbeat_report_download_today(conn, cfg, store):
    vid = add(conn, "a.mkv", size=5_000_000_000)
    r = store.claim("pc")
    assert r["job"]["id"] == vid
    assert (r["download_used"], r["download_limit"]) == (5_000_000_000,
                                                         cfg.daily_download_limit)
    store.downloads.add(1_000)
    hb = store.heartbeat(vid, "pc")
    assert (hb["download_used"], hb["download_limit"]) == (5_000_001_000,
                                                           cfg.daily_download_limit)
    empty = store.claim("pc")  # no work left: still reports the totals
    assert empty["job"] is None and empty["download_used"] == 5_000_001_000


def test_cloud_account_has_its_own_budget(conn, store):
    add(conn, "a.mkv", size=7_000_000_000)
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"] is not None
    assert store.cloud_downloads.used() == 7_000_000_000
    assert store.downloads.used() == 0
    assert store.pc_downloads.used() == 0


def test_cloud_download_quota_blocks_only_cloud(conn, store):
    jid = add(conn, "a.mkv")
    store.claim("cloud-vm", "x265-cloud", "cloud")
    store.report(jid, "cloud-vm", "download_quota", download_account="cloud")
    assert store.cloud_downloads.blocked()
    assert not store.downloads.blocked()
    assert not store.pc_downloads.blocked()


def test_hdr_offered_to_x265_cloud_not_nvenc(conn, store):
    hdr = add(conn, "hdr.mkv", probe=replace(BASE, color_transfer="smpte2084"))
    assert store.claim("pc")["job"] is None
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"]["id"] == hdr


def test_cloud_activity_does_not_refresh_pc_seen(conn, store):
    jid = add(conn, "a.mkv")
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"]["id"] == jid
    store.heartbeat(jid, "cloud-vm", "cloud")
    store.report(jid, "cloud-vm", "failed", download_account="cloud", reason="x")
    assert db.kv_get(conn, PC_SEEN_KEY) is None
    assert db.kv_get(conn, "seen:cloud-vm") == str(store.clock.t)


def test_pc_heartbeat_still_refreshes_pc_seen(conn, store):
    jid = add(conn, "a.mkv")
    store.claim("pc")
    store.clock.t += 100
    store.heartbeat(jid, "pc")
    assert db.kv_get(conn, PC_SEEN_KEY) == str(store.clock.t)


def test_claim_skips_jobs_that_do_not_fit_free_bytes(conn, store, cfg):
    cfg.disk_factor, cfg.disk_reserve = 2.2, 20e9
    add(conn, "big.mkv", size=50_000_000_000)
    small = add(conn, "small.mkv", size=10_000_000_000)
    r = store.claim("cloud-vm", "x265-cloud", "cloud", free_bytes=int(60e9))
    assert r["job"]["id"] == small


def test_claim_no_fit_returns_no_job_and_charges_nothing(conn, store, cfg):
    cfg.disk_factor, cfg.disk_reserve = 2.2, 20e9
    big = add(conn, "big.mkv", size=50_000_000_000)
    r = store.claim("cloud-vm", "x265-cloud", "cloud", free_bytes=int(60e9))
    assert r["job"] is None and r["retry_after"] == NO_WORK_RETRY_SECS
    assert store.cloud_downloads.used() == 0
    assert db.get(conn, big)["stage"] == "queued"


def _short_and_long(conn):
    long_ = add(conn, "long.mkv", size=500_000_000_000,
                probe=replace(BASE, duration=20 * 3600.0))
    short = add(conn, "short.mkv", size=5_000_000_000, probe=replace(BASE, duration=2 * 3600.0))
    return short, long_


def test_claim_max_hours_filters_by_encoder_speed(conn, store):
    short, _ = _short_and_long(conn)
    r = store.claim("cloud-vm", "x265-cloud", "cloud", max_hours=12)
    assert r["job"]["id"] == short
    assert store.claim("cloud-vm", "x265-cloud", "cloud", max_hours=12)["job"] is None


def test_claim_without_filters_is_unchanged(conn, store):
    _, long_ = _short_and_long(conn)
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"]["id"] == long_


def test_alert_is_stored_and_cleared_by_next_claim(conn, store):
    key = f"{ALERT_PREFIX}cloud-vm"
    store.alert("cloud-vm", "re-authorize gdrive:")
    stored = json.loads(db.kv_get(conn, key))
    assert stored["message"] == "re-authorize gdrive:" and stored["t"] == store.clock.t
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"] is None
    assert db.kv_get(conn, key) is not None
    add(conn, "a.mkv")
    assert store.claim("cloud-vm", "x265-cloud", "cloud")["job"] is not None
    assert db.kv_get(conn, key) is None


def test_resumed_claim_clears_alert(conn, store):
    add(conn, "a.mkv")
    store.claim("cloud-vm", "x265-cloud", "cloud")
    store.alert("cloud-vm", "x")
    assert store.claim("cloud-vm", "x265-cloud", "cloud", resume=True)["resumed"] is True
    assert db.kv_get(conn, f"{ALERT_PREFIX}cloud-vm") is None
