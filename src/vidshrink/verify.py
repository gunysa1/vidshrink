import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vidshrink import procflags
from vidshrink.procflags import TEXT_KWARGS

from vidshrink.encode import output_streams
from vidshrink.probe import Probe


@dataclass
class Verdict:
    problems: list[str]
    not_worth_it: bool

    @property
    def ok(self) -> bool:
        return not self.problems and not self.not_worth_it




def compare(src: Probe, out: Probe, src_size: int, out_size: int,
            min_saving: float, decode_errors: str, src_decode_errors: str = "") -> Verdict:
    """`src_decode_errors`: the source's own audio decode errors, if any. Audio is
    copied as-is, so output errors the source already has are not the encode's."""
    problems = []
    # A trailing subtitle or audio packet can stretch a container's duration by a
    # few seconds beyond the video; allow that much on long files.
    if abs(src.duration - out.duration) > max(1.0, src.duration * 0.0005):
        problems.append(f"duration {src.duration:.1f}s -> {out.duration:.1f}s")
    if out.codec != "hevc":
        problems.append(f"output codec is {out.codec}")
    if not _streams_match(output_streams(src.streams), out.streams):
        problems.append(f"streams differ: {src.streams} -> {out.streams}")
    new_errors = unexplained_errors(decode_errors, src_decode_errors)
    if new_errors:
        problems.append("decode errors: " + new_errors[0][:200])
    return Verdict(problems, out_size > src_size * (1 - min_saving))


def _streams_match(expected: list[list[str]], out: list[list[str]]) -> bool:
    """Same kinds and codecs in order. An undetermined source language may come out
    with any tag (Matroska reads a missing language as English)."""
    return len(expected) == len(out) and all(
        e[:2] == o[:2] and (e[2] == o[2] or e[2] in ("und", ""))
        for e, o in zip(expected, out))


# "[aac @ 0x55d0c8]" (Linux) / "[aac @ 000001f4]" (Windows) and "aist#0:1": per-run
# addresses and output stream numbers, which differ between source and output.
# "in stream 1: 112 >= 112": the null muxer's stream number and timestamps, which
# differ too (the source check maps audio only; containers count time differently).
_ERROR_NOISE = re.compile(r"@ (?:0x)?[0-9a-fA-F]+|#\d+:\d+|in stream \d+: \d+ >= \d+")


_REPEATED = re.compile(r"^\s*Last message repeated \d+ times?$")


def unexplained_errors(errors: str, src_errors: str) -> list[str]:
    """The decode error lines of `errors` whose message never appears in `src_errors`.
    A bare "ffmpeg exit N" (no message to match) is never explained away."""
    known = {_ERROR_NOISE.sub("", line) for line in src_errors.splitlines()
             if not line.startswith("ffmpeg exit")}
    return [line for line in errors.strip().splitlines()
            if line.strip() and not _REPEATED.match(line)
            and _ERROR_NOISE.sub("", line) not in known]


def decode_errors(path: Path, audio_only: bool = False) -> str:
    maps = ["-map", "0:a?"] if audio_only else ["-map", "0:v", "-map", "0:a?"]
    # Keep the file's own time base: rounding ms timestamps to frame ticks makes
    # the null muxer report duplicate dts when video starts between frame ticks.
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
         *maps, "-enc_time_base", "demux", "-f", "null", "-"],
        capture_output=True, **TEXT_KWARGS, **procflags.spawn_kwargs(),
    )
    err = res.stderr.strip()
    if res.returncode != 0 and not err:
        err = f"ffmpeg exit {res.returncode}"
    return err


def source_errors(src: Path, out: Path) -> str:
    """The source's audio decode errors, read only when the output's audio has some
    (decoding the whole source would cost far more)."""
    return decode_errors(src, audio_only=True) if decode_errors(out, audio_only=True) else ""
