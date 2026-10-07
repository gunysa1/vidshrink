import sys
import time
from dataclasses import replace

import pytest

from vidshrink.encode import EncodeError, build_command, run_encode, x265_params
from vidshrink.probe import Probe, parse_ffprobe, run_ffprobe

SDR = Probe(container="matroska,webm", duration=10.0, codec="h264", width=1920, height=1080,
            video_index=0, vbitrate=10e6, is_dv=False, color_primaries="bt709",
            color_transfer="bt709", color_space="bt709", master_display=None, max_cll=None,
            streams=[])


def test_x265_params_sdr():
    p = x265_params(SDR)
    assert "hdr10" not in p
    assert "colorprim=bt709" in p


def test_x265_params_hdr10():
    hdr = replace(SDR, width=3840, height=2160, color_primaries="bt2020",
                  color_transfer="smpte2084", color_space="bt2020nc",
                  master_display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", max_cll="1000,400")
    p = x265_params(hdr).split(":")
    assert {"hdr10=1", "hdr10-opt=1", "repeat-headers=1", "colorprim=bt2020",
            "transfer=smpte2084", "colormatrix=bt2020nc",
            "master-display=G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", "max-cll=1000,400"} <= set(p)


def test_x265_params_ignores_unknown_colors():
    assert "colorprim" not in x265_params(replace(SDR, color_primaries="unknown"))


def test_command_uses_4k_crf_and_video_index(cfg, tmp_path):
    probe = replace(SDR, width=3840, height=2160, video_index=1)
    cmd = build_command(tmp_path / "s", tmp_path / "o.mkv", probe, cfg)
    assert cmd[cmd.index("-c:v:1") + 1] == "libx265"
    assert cmd[cmd.index("-crf:v:1") + 1] == "24"


def _probe_file(path):
    return parse_ffprobe(run_ffprobe(str(path)), path.stat().st_size)


def test_encode_mkv_keeps_tracks(cfg, make_clip, tmp_path):
    src = make_clip("src")
    out = tmp_path / "out.mkv"
    probe = _probe_file(src)
    secs = run_encode(build_command(src, out, probe, cfg), tmp_path / "log")
    assert secs > 0
    got = _probe_file(out)
    assert got.codec == "hevc"
    assert got.streams == probe.streams
    raw = run_ffprobe(str(out))
    assert raw["streams"][0]["pix_fmt"] == "yuv420p10le"
    assert out.stat().st_size < src.stat().st_size * 0.8


def test_encode_mp4_converts_mov_text(cfg, make_clip, tmp_path):
    src = make_clip("src", container="mp4")
    out = tmp_path / "out.mkv"
    run_encode(build_command(src, out, _probe_file(src), cfg), tmp_path / "log")
    assert _probe_file(out).streams[-1] == ["subtitle", "subrip", "eng"]


def test_encode_failure_raises(cfg, tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 100)
    with pytest.raises(EncodeError):
        run_encode(build_command(bad, tmp_path / "o.mkv", SDR, cfg), tmp_path / "log")


def test_run_encode_calls_tick_while_waiting(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(1.2)"]
    ticks = []
    secs = run_encode(cmd, tmp_path / "log", tick=lambda: ticks.append(time.monotonic()),
                       tick_secs=0.2)
    assert len(ticks) >= 3
    assert secs == pytest.approx(1.2, abs=0.5)


def test_run_encode_failure_with_tick_still_raises(tmp_path):
    cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]
    with pytest.raises(EncodeError):
        run_encode(cmd, tmp_path / "log", tick=lambda: None, tick_secs=0.2)


def test_run_encode_tick_error_does_not_stop_encode(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(1.0)"]
    calls = []

    def bad_tick():
        calls.append(1)
        raise RuntimeError("boom")

    secs = run_encode(cmd, tmp_path / "log", tick=bad_tick, tick_secs=0.2)
    assert secs == pytest.approx(1.0, abs=0.5)
    assert len(calls) >= 2


def test_run_encode_no_tick_after_process_exits(tmp_path):
    """A tick that overruns and happens to still be running when ffmpeg exits must
    not inflate things further: once the next check sees ffmpeg has exited, no
    further tick is called (encode_secs must not keep growing on a slow tick)."""
    cmd = [sys.executable, "-c", "import time; time.sleep(0.3)"]
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.6)  # long enough that the process exits during this tick

    secs = run_encode(cmd, tmp_path / "log", tick=tick, tick_secs=0.1)
    assert len(calls) == 1  # no tick ran after the process had already exited
    assert secs < 1.2
