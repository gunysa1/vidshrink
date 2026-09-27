import json
import subprocess

import pytest

from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe

HDR_STREAM = {
    "index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
    "color_primaries": "bt2020", "color_transfer": "smpte2084", "color_space": "bt2020nc",
    "disposition": {"attached_pic": 0},
    "side_data_list": [
        {"side_data_type": "Mastering display metadata",
         "red_x": "34000/50000", "red_y": "16000/50000",
         "green_x": "13250/50000", "green_y": "34500/50000",
         "blue_x": "7500/50000", "blue_y": "3000/50000",
         "white_point_x": "15635/50000", "white_point_y": "16450/50000",
         "min_luminance": "50/10000", "max_luminance": "10000000/10000"},
        {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
    ],
}


def test_parse_hdr_side_data():
    data = {"format": {"format_name": "matroska,webm", "duration": "100.0"},
            "streams": [HDR_STREAM]}
    p = parse_ffprobe(data, size=1_000_000_000)
    assert p.is_hdr and not p.is_dv
    assert p.master_display == "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    assert p.max_cll == "1000,400"


def test_parse_skips_cover_art_and_collects_streams():
    data = {
        "format": {"format_name": "matroska,webm", "duration": "10.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "disposition": {"attached_pic": 0}, "tags": {"BPS": "8000000"}},
            {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
            {"codec_type": "subtitle", "codec_name": "subrip"},
            {"codec_type": "attachment", "codec_name": "ttf"},
        ],
    }
    p = parse_ffprobe(data, size=10_000_000)
    assert (p.codec, p.video_index, p.vbitrate) == ("h264", 1, 8_000_000)
    assert p.streams == [["audio", "ac3", "eng"], ["subtitle", "subrip", "und"]]


def test_parse_estimates_bitrate_from_size():
    data = {"format": {"format_name": "avi", "duration": "8.0"},
            "streams": [{"codec_type": "video", "codec_name": "mpeg4", "width": 640, "height": 480}]}
    p = parse_ffprobe(data, size=1_000_000)
    assert p.vbitrate == pytest.approx(900_000)


def test_parse_detects_dolby_vision():
    stream = dict(HDR_STREAM, side_data_list=[{"side_data_type": "DOVI configuration record"}])
    data = {"format": {"format_name": "matroska,webm", "duration": "1"}, "streams": [stream]}
    assert parse_ffprobe(data, size=1).is_dv


def test_parse_rejects_no_video():
    with pytest.raises(ProbeError):
        parse_ffprobe({"format": {"duration": "1"}, "streams": [{"codec_type": "audio"}]}, 1)


def test_json_round_trip():
    data = {"format": {"format_name": "matroska,webm", "duration": "100.0"}, "streams": [HDR_STREAM]}
    p = parse_ffprobe(data, size=5)
    assert Probe.from_json(p.to_json()) == p


def test_run_ffprobe_on_real_clip(make_clip):
    clip = make_clip("a")
    p = parse_ffprobe(run_ffprobe(str(clip)), clip.stat().st_size)
    assert p.codec == "h264" and (p.width, p.height) == (640, 360)
    assert p.duration == pytest.approx(3.0, abs=0.1)
    assert p.streams == [["audio", "aac", "eng"], ["audio", "aac", "heb"], ["subtitle", "subrip", "eng"]]


def test_run_ffprobe_fails_on_garbage(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(ProbeError):
        run_ffprobe(str(bad))


def test_run_ffprobe_handles_malformed_json(monkeypatch):
    import subprocess as sp
    import vidshrink.probe
    monkeypatch.setattr(vidshrink.probe.subprocess, "run",
                        lambda *args, **kwargs: sp.CompletedProcess(args=[], returncode=0, stdout="{not json", stderr=""))
    with pytest.raises(ProbeError, match="bad ffprobe json"):
        run_ffprobe("x")


def test_parse_field_order_of_main_stream():
    base = {"format": {"format_name": "mpeg", "duration": "10.0"}}
    streams = [
        {"codec_type": "video", "codec_name": "mjpeg", "field_order": "progressive",
         "disposition": {"attached_pic": 1}},
        {"codec_type": "video", "codec_name": "mpeg2video", "width": 720, "height": 576,
         "field_order": "tt"},
    ]
    assert parse_ffprobe(dict(base, streams=streams), size=1).field_order == "tt"
    no_key = [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}]
    assert parse_ffprobe(dict(base, streams=no_key), size=1).field_order is None


def test_from_json_without_field_order():
    # Probes stored before field_order existed must still load.
    data = {"format": {"format_name": "matroska,webm", "duration": "100.0"}, "streams": [HDR_STREAM]}
    stored = json.loads(parse_ffprobe(data, size=5).to_json())
    stored.pop("field_order")
    p = Probe.from_json(json.dumps(stored))
    assert p.field_order is None and p.codec == "hevc"


def test_run_ffprobe_reads_field_order(tmp_path):
    clip = tmp_path / "i.mpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=320x240:rate=25:duration=1", "-vf", "setfield=tff",
                    "-c:v", "mpeg2video", "-flags", "+ildct+ilme", str(clip)], check=True)
    assert parse_ffprobe(run_ffprobe(str(clip)), clip.stat().st_size).field_order == "tt"
