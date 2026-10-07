import subprocess
import threading
import time
from pathlib import Path

from vidshrink import procflags
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


class EncodeCancelled(Exception):
    pass


def _input_args(src: Path, probe: Probe) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if probe.container.startswith("avi"):
        cmd += ["-fflags", "+genpts"]
    return cmd + ["-i", str(src), "-map", "0", "-map", "-0:d", "-c", "copy"]


def _output_args(out: Path, probe: Probe) -> list[str]:
    cmd = []
    subs = [s for s in probe.streams if s[0] == "subtitle"]
    for i, (_, codec, _) in enumerate(subs):
        if codec == "mov_text":
            cmd += [f"-c:s:{i}", "srt"]
    return cmd + ["-max_muxing_queue_size", "4096", str(out)]


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
    video = [
        f"-c:v:{vi}", "libx265",
        f"-preset:v:{vi}", cfg.preset,
        f"-crf:v:{vi}", str(crf),
        f"-pix_fmt:v:{vi}", "yuv420p10le",
        f"-x265-params:v:{vi}", x265_params(probe),
    ]
    return _input_args(src, probe) + video + _output_args(out, probe)


def run_encode(cmd: list[str], log_path: Path) -> float:
    start = time.monotonic()
    with open(log_path, "w") as log:
        res = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                             **procflags.spawn_kwargs())
    if res.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        raise EncodeError(f"ffmpeg exit {res.returncode}: {tail}")
    return time.monotonic() - start


# Matrices hevc_nvenc can be tagged with: the rest make the encode output nothing (ffmpeg 9).
NVENC_MATRICES = MATRICES - {"gbr", "bt2020c", "chroma-derived-nc", "chroma-derived-c",
                             "ictcp", "smpte2085", "ycgco"}


def nvenc_color_filter(probe: Probe) -> str | None:
    # ffmpeg 9 takes the encoder's colour tags from the frames, ignoring the
    # -color_primaries/-color_trc output options, so tag the frames with setparams.
    parts = []
    if probe.color_primaries in PRIMARIES:
        parts.append(f"color_primaries={probe.color_primaries}")
    if probe.color_transfer in TRANSFERS:
        parts.append(f"color_trc={probe.color_transfer}")
    if probe.color_space in NVENC_MATRICES:
        parts.append(f"colorspace={probe.color_space}")
    return "setparams=" + ":".join(parts) if parts else None


def build_nvenc_command(src: Path, out: Path, probe: Probe, cq: int) -> list[str]:
    vi = probe.video_index
    color = nvenc_color_filter(probe)
    video = [f"-filter:v:{vi}", color] if color else []
    video += [
        f"-c:v:{vi}", "hevc_nvenc",
        f"-preset:v:{vi}", "p7",
        f"-tune:v:{vi}", "hq",
        f"-profile:v:{vi}", "main10",
        f"-pix_fmt:v:{vi}", "p010le",
        f"-rc:v:{vi}", "vbr",
        f"-cq:v:{vi}", str(cq),
        f"-b:v:{vi}", "0",
        f"-spatial-aq:v:{vi}", "1",
        f"-temporal-aq:v:{vi}", "1",
        f"-rc-lookahead:v:{vi}", "32",
        f"-bf:v:{vi}", "4",
        f"-b_ref_mode:v:{vi}", "middle",
    ]
    return _input_args(src, probe) + video + _output_args(out, probe)


def run_encode_cancellable(cmd: list[str], log_path: Path, cancel: threading.Event) -> float:
    start = time.monotonic()
    with open(log_path, "w") as log:
        # Own process group/session: Ctrl-C in the console reaches only us; we kill
        # ffmpeg ourselves on cancel and release the job.
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                **procflags.spawn_kwargs(new_group=True))
        while True:
            try:
                code = proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise EncodeCancelled("encode cancelled") from None
    if code != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        raise EncodeError(f"ffmpeg exit {code}: {tail}")
    return time.monotonic() - start
