from dataclasses import replace

from vidshrink.probe import Probe
from vidshrink.verify import compare, decode_errors

SRC = Probe(container="mp4", duration=100.0, codec="h264", width=1920, height=1080,
            video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
            color_transfer=None, color_space=None, master_display=None, max_cll=None,
            streams=[["audio", "aac", "eng"], ["subtitle", "mov_text", "eng"]])
OUT = replace(SRC, container="matroska,webm", codec="hevc",
              streams=[["audio", "aac", "eng"], ["subtitle", "subrip", "eng"]])


def test_good_output_passes():
    v = compare(SRC, OUT, 1000, 500, 0.2, "")
    assert v.ok and v.problems == [] and not v.not_worth_it


def test_duration_mismatch():
    assert "duration" in compare(SRC, replace(OUT, duration=98.5), 1000, 500, 0.2, "").problems[0]


def test_missing_track():
    v = compare(SRC, replace(OUT, streams=OUT.streams[:1]), 1000, 500, 0.2, "")
    assert "streams" in v.problems[0]


def test_language_change_detected():
    v = compare(SRC, replace(OUT, streams=[["audio", "aac", "heb"], OUT.streams[1]]),
                1000, 500, 0.2, "")
    assert not v.ok


def test_converted_and_dropped_streams_still_match():
    src = replace(SRC, streams=[["audio", "pcm_bluray", "eng"], ["subtitle", "arib_caption", "jpn"]])
    out = replace(OUT, streams=[["audio", "flac", "eng"]])
    assert compare(src, out, 1000, 500, 0.2, "").ok


def test_not_hevc():
    assert not compare(SRC, replace(OUT, codec="h264"), 1000, 500, 0.2, "").ok


def test_decode_errors_reported():
    v = compare(SRC, OUT, 1000, 500, 0.2, "Invalid NAL unit\nmore")
    assert v.problems == ["decode errors: Invalid NAL unit"]


def test_not_worth_it():
    v = compare(SRC, OUT, 1000, 850, 0.2, "")
    assert v.not_worth_it and not v.ok and v.problems == []


def test_decode_errors_clean_clip(make_clip):
    assert decode_errors(make_clip("a")) == ""


def test_decode_errors_garbage(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 1000)
    assert decode_errors(bad) != ""
