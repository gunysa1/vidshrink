from dataclasses import replace
import subprocess

from vidshrink.probe import Probe
from vidshrink.verify import compare, decode_errors, source_errors

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


def test_streams_reordered_audio_first_still_match():
    # Sin City: a subtitle sits between audio tracks; ffmpeg writes all audio first.
    src = replace(SRC, streams=[["audio", "dts", "eng"], ["subtitle", "hdmv_pgs_subtitle", "eng"],
                                ["audio", "ac3", "pol"]])
    out = replace(OUT, streams=[["audio", "dts", "eng"], ["audio", "ac3", "pol"],
                                ["subtitle", "hdmv_pgs_subtitle", "eng"]])
    assert compare(src, out, 1000, 500, 0.2, "").ok


def test_undetermined_language_may_gain_a_tag():
    src = replace(SRC, streams=[["audio", "ac3", "und"]])
    assert compare(src, replace(OUT, streams=[["audio", "ac3", "eng"]]), 1000, 500, 0.2, "").ok
    # a real language may not change, and a tag may not vanish
    src = replace(SRC, streams=[["audio", "ac3", "eng"]])
    assert not compare(src, replace(OUT, streams=[["audio", "ac3", "und"]]),
                       1000, 500, 0.2, "").ok


def test_duration_tolerance_scales_with_length():
    long_src = replace(SRC, duration=7294.8)  # Ronin: a trailing stream adds 3 s
    assert compare(long_src, replace(OUT, duration=7291.8), 1000, 500, 0.2, "").ok
    short = replace(SRC, duration=1757.6)     # Nurse Jackie: 153 s of video missing
    v = compare(short, replace(OUT, duration=1604.6), 1000, 500, 0.2, "")
    assert v.problems == ["duration 1757.6s -> 1604.6s"]


def test_decode_errors_the_source_already_has_pass():
    out_err = "[aac @ 0xadd7785bf680] env_facs_q 255 is invalid\n"
    src_err = "[aac @ 0000014f568e8940] env_facs_q 255 is invalid\n"  # Windows-style
    assert compare(SRC, OUT, 1000, 500, 0.2, out_err, src_err).ok
    v = compare(SRC, OUT, 1000, 500, 0.2,
                out_err + "[hevc @ 0x55d0] Invalid NAL unit size\n", src_err)
    assert v.problems == ["decode errors: [hevc @ 0x55d0] Invalid NAL unit size"]


def test_decode_errors_stream_labels_ignored():
    out_err = "[aist#0:1/mp3 @ 0xb187] [dec:mp3float @ 0xb18740] Invalid data\n"
    src_err = "[aist#0:2/mp3 @ 0x5512] [dec:mp3float @ 0x9999aa] Invalid data\n"
    assert compare(SRC, OUT, 1000, 500, 0.2, out_err, src_err).ok


def test_decode_errors_source_audio_dts_glitch_passes():
    # SpongeBob S16E15: the source's own audio has a duplicate timestamp. The source
    # check decodes audio only (stream 0); in the output it is stream 1, and an mp4
    # source counts in samples where the mkv output counts in ms.
    src_err = ("[null @ 0x55e8f4] Application provided invalid, non monotonically "
               "increasing dts to muxer in stream 0: 28335104 >= 28335104\n")
    out_err = ("[null @ 00000285d9] Application provided invalid, non monotonically "
               "increasing dts to muxer in stream 1: 590319 >= 590319\n")
    assert compare(SRC, OUT, 1000, 500, 0.2, out_err, src_err).ok
    assert not compare(SRC, OUT, 1000, 500, 0.2, out_err, "").ok


def test_source_errors_skips_source_when_output_audio_clean(make_clip, tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 1000)
    assert source_errors(bad, make_clip("a")) == ""


def test_bare_exit_code_never_explained_by_source():
    v = compare(SRC, OUT, 1000, 500, 0.2, "ffmpeg exit 1", "ffmpeg exit 1")
    assert v.problems == ["decode errors: ffmpeg exit 1"]


def test_repeat_counts_need_not_match():
    out_err = "[aac @ 0xa] env_facs_q 255 is invalid\n    Last message repeated 4 times\n"
    src_err = "[aac @ 0xb] env_facs_q 255 is invalid\n    Last message repeated 2 times\n"
    assert compare(SRC, OUT, 1000, 500, 0.2, out_err, src_err).ok


def test_decode_errors_video_starting_off_the_frame_grid_is_clean(tmp_path):
    # Naruto: 23.976 fps video starts 63 ms after the audio. Rounding its ms
    # timestamps to frame ticks in the null muxer gave "non monotonically
    # increasing dts".
    video = tmp_path / "v.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=24000/1001:duration=2", "-c:v", "libx264",
                    str(video)], check=True)
    shifted = tmp_path / "shifted.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-itsoffset", "0.063", "-i", str(video),
                    "-f", "lavfi", "-i", "sine=duration=2", "-map", "0", "-map", "1",
                    "-c:v", "copy", "-c:a", "aac", str(shifted)], check=True)
    assert decode_errors(shifted) == ""
