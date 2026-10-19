"""UTF-8 decoding of tool output regardless of the OS locale, and the shared
subprocess spawn flags (no console windows on Windows, own process group for the
PC's long-running children)."""
import locale
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from vidshrink import calibrate, encode, probe, procflags, rclone, verify
from vidshrink.probe import run_ffprobe
from vidshrink.rclone import Rclone

HEBREW = "סרט בדיקה"


@pytest.fixture
def cp1252(monkeypatch):
    """Simulate a Windows box whose ANSI code page is cp1252: that's what text-mode
    subprocess pipes decode with when no encoding is given."""
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")


def test_probe_non_ascii_title_and_name_under_cp1252(make_clip, tmp_path, cp1252):
    clip = make_clip("a")
    tagged = tmp_path / f"{HEBREW}.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-map", "0", "-c", "copy",
                    "-metadata", f"title={HEBREW}", str(tagged)], check=True)
    data = run_ffprobe(str(tagged))
    assert data["format"]["tags"]["title"] == HEBREW
    assert data["format"]["filename"] == str(tagged)


@pytest.mark.parametrize("cancellable", [False, True])
def test_rclone_lists_non_ascii_names_under_cp1252(tmp_path, cp1252, cancellable):
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / f"{HEBREW}.mkv").write_bytes(b"x")
    rc = Rclone(str(drive) + "/")
    cancel = threading.Event() if cancellable else None
    out = rc._run("lsjson", "--files-only", "--no-mimetype", rc.remote, cancel=cancel)
    assert f"{HEBREW}.mkv" in out


def test_spawn_kwargs_current_platform():
    if sys.platform == "win32":
        base = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        assert procflags.spawn_kwargs() == {"creationflags": base}
        assert procflags.spawn_kwargs(new_group=True) == {
            "creationflags": base | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert procflags.spawn_kwargs() == {}
        assert procflags.spawn_kwargs(new_group=True) == {"start_new_session": True}


def test_spawn_kwargs_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000, raising=False)
    assert procflags.spawn_kwargs() == {"creationflags": 0x08004000}
    assert procflags.spawn_kwargs(new_group=True) == {"creationflags": 0x08004200}


class _Done:
    returncode = 0
    stdout = "{}"
    stderr = ""

    def communicate(self, timeout=None):
        return "", ""

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass


def _spy(monkeypatch):
    """Record every subprocess.run/Popen call's kwargs; spawn_kwargs returns a marker
    saying which variant was asked for."""
    calls = []

    def run(cmd, **kw):
        calls.append(("run", cmd, kw))
        return _Done()

    def popen(cmd, **kw):
        calls.append(("popen", cmd, kw))
        return _Done()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(procflags, "spawn_kwargs",
                        lambda new_group=False: {"_group": new_group})
    return calls


def test_every_tool_spawn_uses_utf8_and_spawn_kwargs(monkeypatch, tmp_path):
    calls = _spy(monkeypatch)
    rc = Rclone("remote:")
    p = probe.Probe(container="matroska", duration=10.0, codec="h264", width=640, height=360,
                    video_index=0, vbitrate=1e6, is_dv=False, color_primaries=None,
                    color_transfer=None, color_space=None, master_display=None, max_cll=None,
                    streams=[])
    rc._run("lsjson", "x")
    rc._run("copyto", "a", "b", cancel=threading.Event())
    probe.run_ffprobe("x.mkv")
    verify.decode_errors(Path("x.mkv"))
    calibrate.cut_sample(Path("a.mkv"), tmp_path / "b.mkv", p)
    monkeypatch.setattr(calibrate.Path, "read_text", lambda self, **kw: '{"pooled_metrics": '
                        '{"vmaf": {"mean": 99.0}}}')
    calibrate.vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")
    encode.run_encode(["ffmpeg"], tmp_path / "x.log")
    encode.run_encode_cancellable(["ffmpeg"], tmp_path / "y.log", threading.Event())
    assert len(calls) == 8
    text_calls = calls[:6]  # everything but the two encodes reads text from a pipe
    for kind, cmd, kw in text_calls:
        assert (kw.get("text"), kw.get("encoding"), kw.get("errors")) == \
            (True, "utf-8", "replace"), cmd
    groups = [kw.get("_group") for _, _, kw in calls]
    # Long-running PC children (cancellable rclone, cancellable encode) get their own
    # group; everything else just the no-window flag.
    assert groups == [False, True, False, False, False, False, False, True]


def test_serve_http_spawn_uses_spawn_kwargs(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(rclone.socket, "create_connection",
                        lambda *a, **k: type("C", (), {"close": lambda self: None})())
    with rclone.serve_http(Rclone("remote:"), 1):
        pass
    assert [kw.get("_group") for _, _, kw in calls] == [False]
