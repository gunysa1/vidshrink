import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vidshrink import db
from vidshrink.config import Config
from vidshrink.jobs import JobStore, make_server
from vidshrink.rclone import Rclone, free_port


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


@pytest.fixture
def job_service(cfg):
    """HTTP job service running in a background thread with its own connection."""
    TOKEN = "s3cret"
    port = free_port()
    ready_event = threading.Event()
    server_holder = {}  # Use dict to allow closure to write to it
    exception_holder = {}  # Capture exceptions from thread

    def run_server():
        try:
            # Open connection in the server thread (not shared)
            conn = db.connect(cfg.db_path)
            store = JobStore(conn, cfg, Rclone(cfg.remote), log=lambda m: None)
            server = make_server(store, TOKEN, "127.0.0.1", port)
            server_holder['server'] = server
            server_holder['conn'] = conn
            ready_event.set()
            server.serve_forever()
        except Exception as e:
            exception_holder['error'] = e
            ready_event.set()  # Signal that we're done (with error)
        finally:
            # Close connection when serve_forever returns
            if 'conn' in server_holder:
                server_holder['conn'].close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    if not ready_event.wait(timeout=10):
        raise RuntimeError("job_service fixture: server did not start within 10 seconds")
    if 'error' in exception_holder:
        raise RuntimeError(f"job_service fixture: server thread failed: {exception_holder['error']}") from exception_holder['error']

    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", token=TOKEN)

    # Safe teardown when server may not have been created
    if 'server' in server_holder:
        server_holder['server'].shutdown()
        server_holder['server'].server_close()
    t.join(timeout=5)


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
