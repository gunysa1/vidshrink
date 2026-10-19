import shutil
from pathlib import Path

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.cli import main, status_report
from vidshrink.jobs import PC_SEEN_KEY, JobStore


def test_scan_run_status_end_to_end(cfg, make_clip, capsys, monkeypatch):
    # Real `scan` path: rclone serve http + ffprobe over HTTP, against a local "drive".
    # The size floor is 200 MB by default, so the CLI test lowers it via the env override.
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    monkeypatch.setenv("VIDSHRINK_MIN_SIZE", "0")
    common = ["--remote", cfg.remote, "--db", str(cfg.db_path), "--workdir", str(cfg.workdir)]

    assert main(common + ["scan"]) == 0
    assert main(common + ["status"]) == 0
    out = capsys.readouterr().out
    assert "queued" in out and "Projected" in out

    assert main(common + ["run", "--limit", "1"]) == 0
    conn = db.connect(cfg.db_path)
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "done"
    main(common + ["status"])
    assert "Saved so far" in capsys.readouterr().out


def test_status_report_lists_all_swapping_rows(cfg, conn):
    db.upsert_listing(conn, "Movies/b.mkv", 1_000_000_000, "2024-01-01T00:00:00Z")
    vid1 = db.get_by_path(conn, "Movies/b.mkv")["id"]
    db.mark(conn, vid1, "swapping", reason="swap: rclone timeout")

    db.upsert_listing(conn, "Movies/c.mkv", 1_000_000_000, "2024-01-01T00:00:00Z")
    vid2 = db.get_by_path(conn, "Movies/c.mkv")["id"]
    db.mark(conn, vid2, "swapping")

    report = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "Swaps waiting to be retried (2):" in report
    assert "Movies/b.mkv: swap: rclone timeout" in report
    assert "Movies/c.mkv: swap in progress or interrupted" in report


def test_status_report_lists_dry_run_uploads(cfg, conn):
    for path in ("Movies/a.mkv", "TV/S1/e.avi"):
        db.upsert_listing(conn, path, 1_000_000_000, "2024-01-01T00:00:00Z")
        db.mark(conn, db.get_by_path(conn, path)["id"], "dry_run")
    report = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "Dry-run uploads to review (2):" in report
    assert "Movies/a.mkv -> Movies/a.vidshrink-tmp.mkv" in report
    assert "TV/S1/e.avi -> TV/S1/e.vidshrink-tmp.mkv" in report


def test_status_shows_download_budget(conn, cfg):
    downloads = Budget(conn, cfg.daily_download_limit, kind="download")
    downloads.add(42 * 10**9)
    out = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "Downloaded today: 42 / 500 GB" in out and "PAUSED" not in out
    downloads.block(3600)
    assert "PAUSED" in status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))


def test_status_shows_pc_download_budget_when_used(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    pc_downloads = Budget(conn, cfg.daily_pc_download_limit, budget.now, kind="pc_download")
    pc_downloads.add(7 * 10**9)
    out = status_report(conn, cfg, budget)
    assert "PC account downloaded today: 7 GB (no cap)" in out


def test_status_shows_pc_download_budget_when_pc_seen_recently_even_if_unused(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    db.kv_set(conn, PC_SEEN_KEY, str(budget.now()))
    out = status_report(conn, cfg, budget)
    assert "PC account downloaded today: 0 GB (no cap)" in out


def test_status_hides_pc_download_budget_when_never_seen_and_unused(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    out = status_report(conn, cfg, budget)
    assert "PC account downloaded today" not in out


def test_status_hides_pc_download_budget_once_pc_absent_long_enough(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    db.kv_set(conn, PC_SEEN_KEY, str(budget.now() - (cfg.pc_absent_days + 1) * 86400))
    out = status_report(conn, cfg, budget)
    assert "PC account downloaded today" not in out


def test_status_shows_cloud_downloads(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    Budget(conn, cfg.daily_cloud_download_limit, budget.now, kind="cloud_download").add(12 * 10**9)
    out = status_report(conn, cfg, budget)
    assert "Cloud account downloaded today: 12 GB (no cap)" in out and "PAUSED" not in out.split("Cloud")[1]


def test_status_shows_worker_last_seen(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    db.kv_set(conn, "seen:cloud-vm", str(budget.now() - 7200))
    assert "cloud-vm last seen: 2.0 h ago" in status_report(conn, cfg, budget)


def test_status_shows_alerts(conn, cfg):
    JobStore(conn, cfg, None).alert("cloud-vm", "Drive auth expired")
    out = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "ALERT cloud-vm: Drive auth expired" in out


def test_status_has_no_cloud_lines_without_cloud_activity(conn, cfg):
    out = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "Cloud" not in out and "ALERT" not in out and "last seen" not in out


def test_status_labels_are_not_pc_specific(conn, cfg):
    jid = _leased_row(conn)
    db.mark(conn, jid, "remote_ready")
    out = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "Remote results waiting for the server (1):" in out
    db.mark(conn, jid, "leased", worker="cloud-vm", lease_until=10**10)
    out = status_report(conn, cfg, Budget(conn, cfg.daily_upload_limit))
    assert "With remote workers (1):" in out


def test_status_does_not_list_the_pc_twice(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    jid = _leased_row(conn)
    db.mark(conn, jid, "done", worker="pc", encoder="nvenc")
    db.kv_set(conn, PC_SEEN_KEY, str(budget.now() - 3600))
    db.kv_set(conn, "seen:pc", str(budget.now() - 3600))
    db.kv_set(conn, "seen:cloud-vm", str(budget.now() - 7200))
    out = status_report(conn, cfg, budget)
    assert "PC last seen" in out and "pc last seen" not in out
    assert "cloud-vm last seen" in out


def _leased_row(conn):
    db.upsert_listing(conn, "a.mkv", 5, "t")
    return db.get_by_path(conn, "a.mkv")["id"]


def test_status_shows_no_cap(conn, cfg):
    budget = Budget(conn, cfg.daily_upload_limit)
    Budget(conn, cfg.daily_pc_download_limit, budget.now, kind="pc_download").add(168 * 10**9)
    Budget(conn, cfg.daily_cloud_download_limit, budget.now, kind="cloud_download").add(34 * 10**9)
    out = status_report(conn, cfg, budget)
    assert "PC account downloaded today: 168 GB (no cap)" in out
    assert "Cloud account downloaded today: 34 GB (no cap)" in out
