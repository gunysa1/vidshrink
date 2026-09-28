from dataclasses import replace

from vidshrink import db, scorer
from vidshrink.probe import Probe

BASE = Probe(container="matroska,webm", duration=3600.0, codec="h264", width=1920, height=1080,
             video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
             color_transfer=None, color_space=None, master_display=None, max_cll=None,
             streams=[["audio", "ac3", "eng"]])


def test_resolution_class():
    assert scorer.resolution_class(720, 480) == "sd"
    assert scorer.resolution_class(1280, 720) == "720p"
    assert scorer.resolution_class(1920, 800) == "1080p"
    assert scorer.resolution_class(3840, 1600) == "4k"


def test_skip_reasons(cfg):
    cfg = replace(cfg, min_size=200_000_000)
    ok = 5_000_000_000
    assert scorer.skip_reason("Movies/a.mkv", ok, BASE, cfg) is None
    assert "excluded" in scorer.skip_reason("Torrent/a.mkv", ok, BASE, cfg)
    assert "small" in scorer.skip_reason("a.mkv", 100_000_000, BASE, cfg)
    assert "large" in scorer.skip_reason("a.mkv", 130_000_000_000, BASE, cfg)
    assert "efficient" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="hevc"), cfg)
    assert "efficient" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="av1"), cfg)
    assert "unsupported" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="vp9"), cfg)
    assert "dolby" in scorer.skip_reason("a.mkv", ok, replace(BASE, is_dv=True), cfg)
    assert "bitrate" in scorer.skip_reason("a.mkv", ok, replace(BASE, vbitrate=2e6), cfg)
    for order in ("tt", "bb", "tb", "bt"):
        assert scorer.skip_reason("a.mkv", ok, replace(BASE, field_order=order), cfg) == "interlaced"
    for order in (None, "progressive", "unknown"):
        assert scorer.skip_reason("a.mkv", ok, replace(BASE, field_order=order), cfg) is None


def test_score_units():
    # 10 GB * 0.5 saved = 5 GB; 1 h of video * 2.0 speed = 2 h -> 2.5 GB/h
    assert scorer.score(10_000_000_000, BASE, ratio=0.5, speed=2.0) == 2.5


def _add(conn, path, size, probe):
    db.upsert_listing(conn, path, size, "t")
    db.save_probe(conn, path, probe.to_json())


def test_rescore_orders_legacy_above_h264(conn, cfg):
    _add(conn, "h264.mkv", 5_000_000_000, BASE)
    _add(conn, "xvid.avi", 5_000_000_000, replace(BASE, codec="mpeg4"))
    _add(conn, "hevc.mkv", 5_000_000_000, replace(BASE, codec="hevc"))
    scorer.rescore(conn, cfg)
    assert [r["path"] for r in db.rows_in(conn, "queued")] == ["xvid.avi", "h264.mkv"]
    assert db.get_by_path(conn, "hevc.mkv")["stage"] == "skipped"


def test_rescore_uses_learned_values(conn, cfg):
    _add(conn, "a.mkv", 5_000_000_000, BASE)
    scorer.rescore(conn, cfg)
    before = db.get_by_path(conn, "a.mkv")["score"]
    db.learn(conn, "speed:1080p", 0.1)  # encodes much faster than guessed
    scorer.rescore(conn, cfg)
    assert db.get_by_path(conn, "a.mkv")["score"] > before


def test_rescore_leaves_rows_that_moved_on(conn, cfg, monkeypatch):
    # A worker (another process) takes rows between rescore's read and its update.
    _add(conn, "h264.mkv", 5_000_000_000, BASE)
    _add(conn, "hevc.mkv", 5_000_000_000, replace(BASE, codec="hevc"))
    other = db.connect(cfg.db_path)
    real_rows_in = db.rows_in

    def racing(c, *stages):
        rows = real_rows_in(c, *stages)
        for r in rows:
            db.mark(other, r["id"], "downloading")
        return rows

    monkeypatch.setattr(db, "rows_in", racing)
    scorer.rescore(conn, cfg)
    other.close()
    for path in ("h264.mkv", "hevc.mkv"):
        r = db.get_by_path(conn, path)
        assert (r["stage"], r["score"], r["reason"]) == ("downloading", None, None)
