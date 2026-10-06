import subprocess
from dataclasses import dataclass
from pathlib import Path

from vidshrink import procflags
from vidshrink.procflags import TEXT_KWARGS

from vidshrink.probe import Probe


@dataclass
class Verdict:
    problems: list[str]
    not_worth_it: bool

    @property
    def ok(self) -> bool:
        return not self.problems and not self.not_worth_it


def _signature(streams: list[list[str]]) -> list[tuple[str, str, str]]:
    return [(k, "subrip" if c == "mov_text" else c, lang) for k, c, lang in streams]


def compare(src: Probe, out: Probe, src_size: int, out_size: int,
            min_saving: float, decode_errors: str) -> Verdict:
    problems = []
    if abs(src.duration - out.duration) > 1.0:
        problems.append(f"duration {src.duration:.1f}s -> {out.duration:.1f}s")
    if out.codec != "hevc":
        problems.append(f"output codec is {out.codec}")
    if _signature(src.streams) != _signature(out.streams):
        problems.append(f"streams differ: {src.streams} -> {out.streams}")
    if decode_errors.strip():
        problems.append("decode errors: " + decode_errors.strip().splitlines()[0][:200])
    return Verdict(problems, out_size > src_size * (1 - min_saving))


def decode_errors(path: Path) -> str:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
         "-map", "0:v", "-map", "0:a?", "-f", "null", "-"],
        capture_output=True, **TEXT_KWARGS, **procflags.spawn_kwargs(),
    )
    err = res.stderr.strip()
    if res.returncode != 0 and not err:
        err = f"ffmpeg exit {res.returncode}"
    return err
