import subprocess
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.config import Config


@pytest.fixture
def cfg(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    return Config(
        remote=str(drive) + "/",
        db_path=tmp_path / "state.db",
        workdir=tmp_path / "work",
        excluded=("Torrent/",),
        min_size=0,
        rclone_args=(),
        retry_base_delay=0.0,
        disk_reserve=0,
        disk_factor=0.0,
        probe_workers=2,
    )


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n2\n00:00:01,500 --> 00:00:02,500\nworld\n"


@pytest.fixture
def make_clip(tmp_path):
    outdir = tmp_path / "clips"
    outdir.mkdir(exist_ok=True)

    def make(name, *, codec="h264", width=640, height=360, seconds=3,
             audio_langs=("eng", "heb"), sub_langs=("eng",), container="mkv") -> Path:
        out = outdir / f"{name}.{container}"
        srt = outdir / "subs.srt"
        srt.write_text(SRT)
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={width}x{height}:rate=24:duration={seconds}"]
        for i, _ in enumerate(audio_langs):
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}:duration={seconds}"]
        for _ in sub_langs:
            cmd += ["-i", str(srt)]
        for i in range(1 + len(audio_langs) + len(sub_langs)):
            cmd += ["-map", str(i)]
        if codec == "h264":
            cmd += ["-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p"]
        elif codec == "hevc":
            cmd += ["-c:v", "libx265", "-x265-params", "log-level=error"]
        else:
            raise ValueError(codec)
        cmd += ["-c:a", "aac", "-c:s", "mov_text" if container == "mp4" else "srt"]
        for i, lang in enumerate(audio_langs):
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
        for i, lang in enumerate(sub_langs):
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
        cmd.append(str(out))
        subprocess.run(cmd, check=True)
        return out

    return make
