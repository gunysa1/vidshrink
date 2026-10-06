"""Pick the NVENC CQ that matches the server's x265 CRF 22 picture quality (VMAF)."""
import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from statistics import mean

from vidshrink import procflags
from vidshrink.encode import EncodeError
from vidshrink.procflags import TEXT_KWARGS
from vidshrink.probe import Probe


def vmaf(distorted: Path, reference: Path, threads: int = 4) -> float:
    with tempfile.TemporaryDirectory() as d:
        graph = ("[0:v]format=yuv420p10le,setpts=PTS-STARTPTS[d];"
                 "[1:v]format=yuv420p10le,setpts=PTS-STARTPTS[r];"
                 f"[d][r]libvmaf=n_threads={threads}:log_fmt=json:log_path=vmaf.json")
        res = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                              "-i", str(distorted.resolve()), "-i", str(reference.resolve()),
                              "-lavfi", graph, "-f", "null", "-"],
                             capture_output=True, cwd=d, **TEXT_KWARGS,
                             **procflags.spawn_kwargs())
        if res.returncode != 0:
            raise EncodeError(f"vmaf failed: {res.stderr[-300:]}")
        log = Path(d) / "vmaf.json"
        return float(json.loads(log.read_text())["pooled_metrics"]["vmaf"]["mean"])


def cut_sample(src: Path, out: Path, probe: Probe, seconds: int = 60) -> None:
    start = max(0.0, probe.duration / 2 - seconds / 2)
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
                          "-ss", f"{start:.1f}", "-i", str(src), "-t", str(seconds),
                          "-map", f"0:v:{probe.video_index}", "-c", "copy", str(out)],
                         capture_output=True, **TEXT_KWARGS, **procflags.spawn_kwargs())
    if res.returncode != 0:
        raise EncodeError(f"cut failed: {res.stderr[-300:]}")


def measure(samples: list[tuple[Path, Probe]], cqs: list[int],
            encode_nvenc: Callable[[int], Callable], encode_x265: Callable, workdir: Path,
            log: Callable[[str], None] = print) -> list[dict]:
    rows = []
    never = threading.Event()
    for src, probe in samples:
        name = src.stem
        sample_probe = Probe(**{**probe.__dict__, "video_index": 0, "streams": []})
        runs = [("x265", None, encode_x265)] + [("nvenc", cq, encode_nvenc(cq)) for cq in cqs]
        for encoder, cq, fn in runs:
            out = workdir / f"cal-{name}-{encoder}-{cq}.mkv"
            try:
                fn(src, out, sample_probe, workdir / "cal.log", never)
                score = vmaf(out, src)
                rows.append({"sample": name, "encoder": encoder, "cq": cq, "vmaf": score,
                             "size": out.stat().st_size})
                log(f"{name}: {encoder} {'' if cq is None else f'CQ {cq} '}VMAF {score:.2f}, "
                    f"{out.stat().st_size / 1e6:.0f} MB")
            finally:
                out.unlink(missing_ok=True)
    return rows


def pick_cq(rows: list[dict], tolerance: float = 0.5) -> int:
    target = mean(r["vmaf"] for r in rows if r["encoder"] == "x265") - tolerance
    by_cq: dict[int, list[float]] = {}
    for r in rows:
        if r["encoder"] == "nvenc":
            by_cq.setdefault(r["cq"], []).append(r["vmaf"])
    good = [cq for cq, v in by_cq.items() if mean(v) >= target]
    return max(good) if good else min(by_cq)


def format_table(rows: list[dict]) -> str:
    lines = [f"{'sample':<30} {'encoder':<7} {'CQ':>3} {'VMAF':>6} {'MB':>7}"]
    for r in rows:
        cq = "" if r["cq"] is None else str(r["cq"])
        lines.append(f"{r['sample'][:30]:<30} {r['encoder']:<7} {cq:>3} {r['vmaf']:6.2f} "
                     f"{r['size'] / 1e6:7.1f}")
    return "\n".join(lines)
