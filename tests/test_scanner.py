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
    assert counts["gone"] == 1 and calls == []
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "gone"


def test_http_prober(cfg, make_clip):
    root = Path(cfg.remote.rstrip("/"))
    shutil.copy(make_clip("h264"), root / "a b [x].mkv")
    rc = Rclone(cfg.remote)
    with serve_http(rc, free_port()) as base:
        p = http_prober(base)("a b [x].mkv", (root / "a b [x].mkv").stat().st_size)
    assert p.codec == "h264"
