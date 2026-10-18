import threading
from dataclasses import replace

import pytest

from vidshrink.encode import (EncodeCancelled, EncodeError, build_nvenc_command,
                              run_encode_cancellable)
from vidshrink.probe import Probe, parse_ffprobe, run_ffprobe

P = Probe(container="matroska,webm", duration=10.0, codec="h264", width=1920, height=1080,
          video_index=1, vbitrate=10e6, is_dv=False, color_primaries="bt709",
          color_transfer="bt709", color_space="bt709", master_display=None, max_cll=None,
          streams=[["audio", "aac", "eng"], ["subtitle", "mov_text", "eng"]])


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_nvenc_command(tmp_path):
    cmd = build_nvenc_command(tmp_path / "s", tmp_path / "o.mkv", P, cq=23)
    assert opt(cmd, "-c:v:1") == "hevc_nvenc"
    assert opt(cmd, "-preset:v:1") == "p7"
    assert opt(cmd, "-tune:v:1") == "hq"
    assert opt(cmd, "-profile:v:1") == "main10"
    assert opt(cmd, "-pix_fmt:v:1") == "p010le"
    assert opt(cmd, "-rc:v:1") == "vbr"
    assert opt(cmd, "-cq:v:1") == "23"
    assert opt(cmd, "-b:v:1") == "0"
    assert opt(cmd, "-bf:v:1") == "4"
    assert opt(cmd, "-b_ref_mode:v:1") == "middle"
    assert opt(cmd, "-spatial-aq:v:1") == "1" and opt(cmd, "-temporal-aq:v:1") == "1"
    assert opt(cmd, "-rc-lookahead:v:1") == "32"
    assert opt(cmd, "-c:s:0") == "srt"  # mov_text converted, as for x265
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"] == ["0:v", "0:a?", "0:s?", "0:t?"]
    assert cmd[-1] == str(tmp_path / "o.mkv")


def test_nvenc_command_avi_genpts(tmp_path):
    cmd = build_nvenc_command(tmp_path / "s", tmp_path / "o.mkv", replace(P, container="avi"), 23)
    assert opt(cmd, "-fflags") == "+genpts"


def x265_cmd(src, out):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
            "-map", "0", "-c", "copy", "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "log-level=error", str(out)]


def test_cancellable_encode_completes(make_clip, tmp_path):
    src = make_clip("a")
    secs = run_encode_cancellable(x265_cmd(src, tmp_path / "o.mkv"), tmp_path / "log",
                                  threading.Event())
    assert secs > 0
    assert parse_ffprobe(run_ffprobe(str(tmp_path / "o.mkv")), 1).codec == "hevc"


def test_cancellable_encode_cancels(make_clip, tmp_path):
    src = make_clip("long", seconds=120, width=1280, height=720)
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    with pytest.raises(EncodeCancelled):
        run_encode_cancellable(x265_cmd(src, tmp_path / "o.mkv"), tmp_path / "log", cancel)


def test_cancellable_encode_failure(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 100)
    with pytest.raises(EncodeError):
        run_encode_cancellable(x265_cmd(bad, tmp_path / "o.mkv"), tmp_path / "log",
                               threading.Event())


def test_nvenc_command_tags_colour_via_setparams(tmp_path):
    cmd = build_nvenc_command(tmp_path / "s", tmp_path / "o.mkv", P, cq=23)
    assert opt(cmd, "-filter:v:1") == \
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"


def test_nvenc_command_skips_unknown_or_unsafe_colour(tmp_path):
    untagged = replace(P, color_primaries=None, color_transfer="unknown", color_space="ycgco")
    cmd = build_nvenc_command(tmp_path / "s", tmp_path / "o.mkv", untagged, cq=23)
    assert "-filter:v:1" not in cmd
    pal = replace(P, color_primaries="bt470bg", color_transfer=None, color_space="gbr")
    cmd = build_nvenc_command(tmp_path / "s", tmp_path / "o.mkv", pal, cq=23)
    assert opt(cmd, "-filter:v:1") == "setparams=color_primaries=bt470bg"
