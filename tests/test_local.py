import shutil
import threading

from vidshrink.encode import run_encode_cancellable
from vidshrink.local import LOG_NAME, ORIGINALS, TMP_SUFFIX, compress_folder
from vidshrink.probe import parse_ffprobe, run_ffprobe


def x265(src, out, probe, log_path, cancel, extra=()):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
           "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-c:s", "srt",
           "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
           *extra, str(out)]
    return run_encode_cancellable(cmd, log_path, cancel)


def codec(path):
    return parse_ffprobe(run_ffprobe(str(path)), path.stat().st_size).codec


def folder_with(tmp_path, make_clip, *clips):
    d = tmp_path / "videos"
    d.mkdir()
    for name, kw in clips:
        src = make_clip(name, **kw)
        shutil.copy(src, d / src.name)
    return d


def test_compresses_and_moves_original_aside(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))
    size = (d / "a.mp4").stat().st_size
    totals = compress_folder(d, x265, log=lambda m: None)
    assert (totals.done, totals.failed) == (1, 0)
    assert not (d / "a.mp4").exists()
    assert (d / ORIGINALS / "a.mp4").stat().st_size == size  # untouched, set aside
    assert codec(d / "a.mkv") == "hevc"
    assert totals.saved == size - (d / "a.mkv").stat().st_size
    assert not list(d.glob(f"*{TMP_SUFFIX}")) and not list(d.glob("*.vidshrink-encode.log"))
    assert "done: a.mp4 -> a.mkv" in (d / LOG_NAME).read_text()


def test_hevc_is_skipped_untouched(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("h", {"codec": "hevc"}))
    totals = compress_folder(d, x265, log=lambda m: None)
    assert (totals.done, totals.skipped) == (0, 1)
    assert (d / "h.mkv").exists() and not (d / ORIGINALS).exists()


def test_failed_check_keeps_original(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))

    def short(src, out, probe, log_path, cancel):
        return x265(src, out, probe, log_path, cancel, extra=("-t", "1"))

    totals = compress_folder(d, short, log=lambda m: None)
    assert (totals.done, totals.failed) == (0, 1)
    assert (d / "a.mp4").exists() and not (d / "a.mkv").exists()
    assert not list(d.glob(f"*{TMP_SUFFIX}"))
    assert "kept original (duration" in (d / LOG_NAME).read_text()


def test_not_worth_it_keeps_original(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))
    totals = compress_folder(d, x265, min_saving=0.9999, log=lambda m: None)
    assert (totals.done, totals.skipped, totals.failed) == (0, 1, 0)
    assert (d / "a.mp4").exists() and not (d / "a.mkv").exists()


def test_existing_mkv_name_is_not_overwritten(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))
    (d / "a.mkv").write_bytes(b"someone else's file")
    totals = compress_folder(d, x265, log=lambda m: None)
    assert totals.done == 0
    assert (d / "a.mkv").read_bytes() == b"someone else's file"
    assert (d / "a.mp4").exists()


def test_dry_run_and_stale_tmp(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))
    (d / f"b{TMP_SUFFIX}").write_bytes(b"left from a crash")
    calls = []
    totals = compress_folder(d, lambda *a: calls.append(a), dry_run=True,
                             log=lambda m: None)
    assert calls == [] and totals.done == 0
    assert (d / "a.mp4").exists()
    assert not (d / f"b{TMP_SUFFIX}").exists()  # never a candidate, and cleaned up


def test_cancel_leaves_original(tmp_path, make_clip):
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4"}))
    cancel = threading.Event()

    def cancelled(src, out, probe, log_path, c):
        out.write_bytes(b"partial")
        cancel.set()
        from vidshrink.encode import EncodeCancelled
        raise EncodeCancelled("stop")

    totals = compress_folder(d, cancelled, cancel=cancel, log=lambda m: None)
    assert totals.done == 0
    assert (d / "a.mp4").exists() and not list(d.glob(f"*{TMP_SUFFIX}"))


def test_cli_local_without_pc_config_uses_x265(tmp_path, make_clip):
    from vidshrink.cli import main
    d = folder_with(tmp_path, make_clip, ("a", {"container": "mp4", "seconds": 1}))
    assert main(["local", str(d), "--config", str(tmp_path / "none.toml")]) == 0
    assert codec(d / "a.mkv") == "hevc" and (d / ORIGINALS / "a.mp4").exists()
