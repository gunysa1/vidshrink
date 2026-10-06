import threading

from vidshrink.calibrate import cut_sample, format_table, measure, pick_cq, vmaf
from vidshrink.encode import run_encode_cancellable
from vidshrink.probe import parse_ffprobe, run_ffprobe


def rows(x265, nvenc):
    r = [{"sample": "s", "encoder": "x265", "cq": None, "vmaf": v, "size": 1} for v in x265]
    for cq, v in nvenc:
        r.append({"sample": "s", "encoder": "nvenc", "cq": cq, "vmaf": v, "size": 1})
    return r


def test_pick_cq_highest_within_tolerance():
    assert pick_cq(rows([95.0], [(20, 97.0), (22, 95.8), (24, 94.6), (26, 92.0)])) == 24


def test_pick_cq_falls_back_to_lowest():
    assert pick_cq(rows([96.0], [(20, 93.0), (22, 92.0)])) == 20


def test_vmaf_identical_is_high(make_clip):
    clip = make_clip("v", seconds=2)
    assert vmaf(clip, clip, threads=2) > 95


def crf_encoder(crf):
    def enc(src, out, probe, log_path, cancel):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
               "-c:v", "libx265", "-preset", "ultrafast", "-crf", str(crf),
               "-x265-params", "log-level=error", str(out)]
        return run_encode_cancellable(cmd, log_path, cancel)
    return enc


def test_measure_shape(make_clip, tmp_path):
    clip = make_clip("m", seconds=3, audio_langs=(), sub_langs=())
    probe = parse_ffprobe(run_ffprobe(str(clip)), clip.stat().st_size)
    sample = tmp_path / "sample.mkv"
    cut_sample(clip, sample, probe, seconds=2)
    nvenc = lambda cq: crf_encoder(cq)  # stand-in: CQ value used as CRF
    out = measure([(sample, probe)], [20, 30], nvenc, crf_encoder(22), tmp_path, log=lambda m: None)
    assert [(r["encoder"], r["cq"]) for r in out] == [("x265", None), ("nvenc", 20), ("nvenc", 30)]
    assert out[1]["vmaf"] > out[2]["vmaf"]
    assert "x265" in format_table(out)
