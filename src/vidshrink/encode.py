import subprocess
import time
from pathlib import Path

from vidshrink.config import Config
from vidshrink.probe import Probe
from vidshrink.scorer import resolution_class

PRIMARIES = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020",
             "smpte428", "smpte431", "smpte432"}
TRANSFERS = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "linear",
             "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12",
             "smpte2084", "smpte428", "arib-std-b67"}
MATRICES = {"gbr", "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco",
            "bt2020nc", "bt2020c", "smpte2085", "chroma-derived-nc", "chroma-derived-c", "ictcp"}


class EncodeError(Exception):
    pass


def x265_params(probe: Probe) -> str:
    parts = ["log-level=error"]
    if probe.color_primaries in PRIMARIES:
        parts.append(f"colorprim={probe.color_primaries}")
    if probe.color_transfer in TRANSFERS:
        parts.append(f"transfer={probe.color_transfer}")
    if probe.color_space in MATRICES:
        parts.append(f"colormatrix={probe.color_space}")
    if probe.is_hdr:
        parts += ["hdr10=1", "hdr10-opt=1", "repeat-headers=1"]
        if probe.master_display:
            parts.append(f"master-display={probe.master_display}")
        if probe.max_cll:
            parts.append(f"max-cll={probe.max_cll}")
    return ":".join(parts)


def build_command(src: Path, out: Path, probe: Probe, cfg: Config) -> list[str]:
    vi = probe.video_index
    crf = cfg.crf_4k if resolution_class(probe.width, probe.height) == "4k" else cfg.crf
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if probe.container.startswith("avi"):
        cmd += ["-fflags", "+genpts"]
    cmd += [
        "-i", str(src),
        "-map", "0", "-map", "-0:d",
        "-c", "copy",
        f"-c:v:{vi}", "libx265",
        f"-preset:v:{vi}", cfg.preset,
        f"-crf:v:{vi}", str(crf),
        f"-pix_fmt:v:{vi}", "yuv420p10le",
        f"-x265-params:v:{vi}", x265_params(probe),
    ]
    subs = [s for s in probe.streams if s[0] == "subtitle"]
    for i, (_, codec, _) in enumerate(subs):
        if codec == "mov_text":
            cmd += [f"-c:s:{i}", "srt"]
    cmd += ["-max_muxing_queue_size", "4096", str(out)]
    return cmd


def run_encode(cmd: list[str], log_path: Path) -> float:
    start = time.monotonic()
    with open(log_path, "w") as log:
        res = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if res.returncode != 0:
        tail = log_path.read_text(errors="replace")[-500:]
        raise EncodeError(f"ffmpeg exit {res.returncode}: {tail}")
    return time.monotonic() - start
