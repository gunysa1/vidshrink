import shutil
from pathlib import Path

from vidshrink import db
from vidshrink.probe import ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, free_port, serve_http
from vidshrink.scanner import http_prober, scan


def local_prober(root):
    def probe(path, size):
        return parse_ffprobe(run_ffprobe(str(root / path)), size)
    return probe


def setup_drive(cfg, make_clip):
    root = cfg.remote.rstrip("/")
    root = Path(root)
    (root / "Movies").mkdir()
    (root / "Torrent").mkdir()
    shutil.copy(make_clip("h264"), root / "Movies" / "a.mkv")
    shutil.copy(make_clip("hevc", codec="hevc"), root / "Movies" / "b.mkv")
    shutil.copy(make_clip("t"), root / "Torrent" / "c.mkv")
    (root / "Movies" / "broken.mkv").write_bytes(b"junk" * 100)
    return root


def test_scan_probes_scores_and_skips(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    counts = scan(conn, Rclone(cfg.remote), cfg, local_prober(root), log=lambda s: None)
    assert counts == {"listed": 4, "gone": 0, "probed": 2, "probe_failed": 1, "excluded": 1}
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "queued"
    assert db.get_by_path(conn, "Movies/b.mkv")["stage"] == "skipped"
    assert db.get_by_path(conn, "Torrent/c.mkv")["stage"] == "skipped"
    assert db.get_by_path(conn, "Movies/broken.mkv")["stage"] == "probe_failed"


def test_rescan_only_probes_changed(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    rc = Rclone(cfg.remote)
    scan(conn, rc, cfg, local_prober(root), log=lambda s: None)
    calls = []

    def counting(path, size):
        calls.append(path)
        raise ProbeError("should not be called for unchanged files")

    (root / "Movies" / "a.mkv").unlink()
    counts = scan(conn, rc, cfg, counting, log=lambda s: None)
    # Only the probe_failed file is tried again (I7); unchanged probed files are not.
    assert counts["gone"] == 1 and calls == ["Movies/broken.mkv"]
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "gone"


def test_http_prober(cfg, make_clip):
    root = Path(cfg.remote.rstrip("/"))
    shutil.copy(make_clip("h264"), root / "a b [x].mkv")
    rc = Rclone(cfg.remote)
    with serve_http(rc, free_port()) as base:
        p = http_prober(base)("a b [x].mkv", (root / "a b [x].mkv").stat().st_size)
    assert p.codec == "h264"


def test_scan_catches_probe_exceptions(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    local = local_prober(root)

    def mixed_prober(path, size):
        if path == "Movies/broken.mkv":
            raise ValueError("bad width")
        return local(path, size)

    counts = scan(conn, Rclone(cfg.remote), cfg, mixed_prober, log=lambda s: None)
    assert counts["probe_failed"] == 1  # broken.mkv raises ValueError
    assert db.get_by_path(conn, "Movies/broken.mkv")["stage"] == "probe_failed"
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "queued"
    assert db.get_by_path(conn, "Movies/b.mkv")["stage"] == "skipped"


def test_scan_skips_duplicate_names(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    rc = Rclone(cfg.remote)
    real_list = rc.list_videos
    probed = []
    local = local_prober(root)

    def prober(path, size):
        probed.append(path)
        return local(path, size)

    def with_dup(exts):
        entries = real_list(exts)
        return entries + [e for e in entries if e["Path"] == "Movies/a.mkv"]

    rc.list_videos = with_dup
    scan(conn, rc, cfg, prober, log=lambda s: None)
    r = db.get_by_path(conn, "Movies/a.mkv")
    assert (r["stage"], r["reason"]) == ("skipped", "duplicate name on Drive")
    assert "Movies/a.mkv" not in probed

    rc.list_videos = real_list  # the duplicate was cleaned up on Drive
    scan(conn, rc, cfg, prober, log=lambda s: None)
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "queued"


def test_rescan_retries_probe_failures(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    rc = Rclone(cfg.remote)
    local = local_prober(root)

    def flaky(path, size):
        if path == "Movies/a.mkv":
            raise ProbeError("connection reset by peer")  # transient, file unchanged
        return local(path, size)

    scan(conn, rc, cfg, flaky, log=lambda s: None)
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "probe_failed"
    counts = scan(conn, rc, cfg, local, log=lambda s: None)
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "queued"
    assert counts["probed"] == 1 and counts["probe_failed"] == 1  # a.mkv now; broken.mkv again
