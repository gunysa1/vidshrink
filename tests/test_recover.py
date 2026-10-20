import hashlib
import shutil
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import Probe, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError
from vidshrink.worker import SwapError, Worker

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
        # Record the original as the scan would have seen it, so the swap's
        # "is this still the file we encoded?" check matches the file on disk.
        listed = worker.rclone.entries(path)
        size, modtime = (listed[0]["Size"], listed[0]["ModTime"]) if listed else (1000, "t")
        db.upsert_listing(conn, path, size, modtime)
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


INTERRUPTED = "interrupted during encoding"


def test_first_interruption_is_free(conn, cfg, setup):
    root, worker, row = setup
    vid = row("e.mkv", "encoding")
    (cfg.workdir / f"{vid}.src").write_bytes(b"partial")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"], r["reason"]) == ("queued", 0, INTERRUPTED)
    assert not (cfg.workdir / f"{vid}.src").exists()


def test_recover_keeps_complete_download(conn, cfg, setup):
    root, worker, row = setup
    vid = row("d.mkv", "downloaded", size=7)
    (cfg.workdir / f"{vid}.src").write_bytes(b"x" * 7)
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"], r["reason"]) == ("downloaded", 0, None)
    assert (cfg.workdir / f"{vid}.src").exists()


def test_recover_requeues_truncated_download(conn, cfg, setup):
    root, worker, row = setup
    vid = row("d.mkv", "downloaded", size=7)
    (cfg.workdir / f"{vid}.src").write_bytes(b"xxx")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"], r["reason"]) == \
        ("queued", 0, "interrupted during downloaded")
    assert not (cfg.workdir / f"{vid}.src").exists()


@pytest.mark.parametrize("stage", ["encoding", "verifying"])
def test_recover_interrupted_encode_with_complete_src_goes_to_downloaded(conn, cfg, setup,
                                                                         stage):
    root, worker, row = setup
    vid = row("e.mkv", stage, size=7)
    (cfg.workdir / f"{vid}.src").write_bytes(b"x" * 7)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(b"partial")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"], r["reason"]) == \
        ("downloaded", 0, f"interrupted during {stage}")
    assert (cfg.workdir / f"{vid}.src").exists()
    assert not (cfg.workdir / f"{vid}.out.mkv").exists()
    # Interrupted again before finishing: now it costs an attempt and starts over.
    db.mark(conn, vid, stage)
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"]) == ("queued", 1)
    assert not (cfg.workdir / f"{vid}.src").exists()


class Crash(BaseException):
    """Stands in for the process dying: nothing in the worker catches it."""


def test_crash_loop_is_parked(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(b"x" * 1000)
    vid = row("e.mkv", "queued")

    def download(*a, **k):
        raise Crash()                      # the machine dies mid-download

    worker.rclone.download = download
    seen = []
    for _ in range(3):
        assert worker.start_download(db.get(conn, vid))
        with pytest.raises(Crash):
            worker._download(db.get(conn, vid))
        assert db.get(conn, vid)["stage"] == "downloading"
        worker.recover()                   # service restarts after the crash
        r = db.get(conn, vid)
        seen.append((r["stage"], r["attempts"]))
    # The first interruption is free, the second in a row burns an attempt, and a
    # file that keeps killing the machine ends up parked.
    assert seen == [("queued", 0), ("queued", 1), ("failed", 2)]


def test_interruption_marker_survives_until_work_completes(conn, cfg, setup, make_clip):
    root, worker, row = setup
    shutil.copy(make_clip("e", container="mp4"), root / "e.mp4")
    vid = row("e.mp4", "queued", reason=INTERRUPTED)
    # row() stores a stub probe; the encode and its verification need the real one.
    db.save_probe(conn, "e.mp4", parse_ffprobe(run_ffprobe(str(root / "e.mp4")),
                                               (root / "e.mp4").stat().st_size).to_json())
    db.mark(conn, vid, "queued", reason=INTERRUPTED)
    real_download = worker.rclone.download
    seen = []

    def download(*a, **k):
        seen.append(db.get(conn, vid)["reason"])
        return real_download(*a, **k)

    worker.rclone.download = download
    assert worker.start_download(db.get(conn, vid))
    worker._download(db.get(conn, vid))
    assert worker.finish_download(db.get(conn, vid))
    assert db.get(conn, vid)["reason"] == INTERRUPTED
    worker.encode_and_verify(db.get(conn, vid))
    assert seen == [INTERRUPTED]                  # still marked while work is under way
    r = db.get(conn, vid)
    assert r["stage"] == "ready" and r["reason"] is None


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

    monkeypatch.setattr(worker.rclone, "entries", raise_err)
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


@pytest.mark.parametrize("dup", ["e.avi", "e.vidshrink-tmp.mkv"])
def test_swap_refuses_duplicate_names(conn, setup, monkeypatch, dup):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    real = worker.rclone.entries

    def doubled(path):
        found = real(path)
        return found * 2 if path == dup else found

    monkeypatch.setattr(worker.rclone, "entries", doubled)
    with pytest.raises(SwapError):
        worker._swap(db.get(conn, vid))
    assert sorted(p.name for p in root.iterdir()) == ["e.avi", "e.vidshrink-tmp.mkv"]
    assert (root / "e.avi").read_bytes() == b"old"
    assert db.get(conn, vid)["stage"] == "swapping"


@pytest.mark.parametrize("final_content", [None, b"foreign content"])
def test_swap_keeps_only_copy_when_original_gone(conn, cfg, setup, final_content):
    # Original already trashed, temp file gone, final absent or not our encode:
    # the local output may be the only good copy, so it must be kept.
    root, worker, row = setup
    if final_content is not None:
        (root / "e.mkv").write_bytes(final_content)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(NEW)
    worker.recover()
    r = db.get(conn, vid)
    assert r["stage"] == "swapping" and r["reason"].startswith("swap: needs attention:")
    assert (cfg.workdir / f"{vid}.out.mkv").read_bytes() == NEW


def test_dry_run_leaves_pending_swaps_alone(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    assert worker.run(dry_run=True) == 0
    assert (root / "e.avi").read_bytes() == b"old"
    assert (root / "e.vidshrink-tmp.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "swapping"
