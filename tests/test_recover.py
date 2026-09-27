import hashlib
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone, RcloneError
from vidshrink.worker import Worker

PROBE = Probe(container="matroska,webm", duration=10.0, codec="h264", width=1920, height=1080,
              video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
              color_transfer=None, color_space=None, master_display=None, max_cll=None,
              streams=[])
NEW = b"new encode"
NEW_MD5 = hashlib.md5(NEW).hexdigest()


@pytest.fixture
def setup(conn, cfg):
    root = Path(cfg.remote.rstrip("/"))
    worker = Worker(conn, Rclone(cfg.remote), cfg, Budget(conn, cfg.daily_upload_limit),
                    sleep=lambda s: None, log=lambda s: None)
    cfg.workdir.mkdir(parents=True, exist_ok=True)

    def row(path, stage, **fields):
        db.upsert_listing(conn, path, 1000, "t")
        db.save_probe(conn, path, PROBE.to_json())
        vid = db.get_by_path(conn, path)["id"]
        db.mark(conn, vid, stage, **fields)
        return vid

    return root, worker, row


SWAP_FIELDS = dict(out_size=len(NEW), out_md5=NEW_MD5, encode_secs=5.0)


def test_swap_both_present(conn, setup):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert sorted(p.name for p in root.iterdir()) == ["e.mkv"]
    assert (root / "e.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_original_already_trashed(conn, setup):
    root, worker, row = setup
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert (root / "e.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_already_finished(conn, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(NEW)
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_inconsistent_marks_failed(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(b"old original, never swapped")
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(NEW)
    worker.recover()
    r = db.get(conn, vid)
    assert r["stage"] == "failed" and r["reason"].startswith("swap:")
    assert (root / "e.mkv").read_bytes() == b"old original, never swapped"
    assert not (cfg.workdir / f"{vid}.out.mkv").exists()


def test_interrupted_encode_requeues_then_fails(conn, cfg, setup):
    root, worker, row = setup
    vid = row("e.mkv", "encoding")
    (cfg.workdir / f"{vid}.src").write_bytes(b"partial")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"]) == ("queued", 1)
    assert not (cfg.workdir / f"{vid}.src").exists()
    db.mark(conn, vid, "verifying")
    worker.recover()
    assert db.get(conn, vid)["stage"] == "failed"


def test_interrupted_upload(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.vidshrink-tmp.mkv").write_bytes(b"half")
    vid = row("e.mkv", "uploading", **SWAP_FIELDS)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(NEW)
    worker.recover()
    assert db.get(conn, vid)["stage"] == "ready"
    assert not (root / "e.vidshrink-tmp.mkv").exists()

    vid2 = row("f.mkv", "uploading", **SWAP_FIELDS)
    worker.recover()
    assert db.get(conn, vid2)["stage"] == "queued"


def test_swap_transient_rclone_error_stays_swapping(conn, cfg, setup, monkeypatch):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(NEW)

    def raise_err(path):
        raise RcloneError(1, "connection reset")

    monkeypatch.setattr(worker.rclone, "exists", raise_err)
    worker.recover()

    r = db.get(conn, vid)
    assert r["stage"] == "swapping" and r["reason"].startswith("swap:")
    assert (cfg.workdir / f"{vid}.out.mkv").exists()
    assert (root / "e.avi").read_bytes() == b"old"
    assert (root / "e.vidshrink-tmp.mkv").read_bytes() == NEW


def test_swap_original_different_ext_only_tmp_present(conn, setup):
    root, worker, row = setup
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert sorted(p.name for p in root.iterdir()) == ["e.mkv"]
    assert (root / "e.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_target_taken_during_recovery(conn, setup):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old original")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    (root / "e.mkv").write_bytes(b"foreign content")
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    r = db.get(conn, vid)
    assert r["stage"] == "failed" and r["reason"].startswith("swap:")
    assert (root / "e.avi").read_bytes() == b"old original"
    assert (root / "e.mkv").read_bytes() == b"foreign content"
