import re
import subprocess
from collections import Counter
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
            min_saving: float, decode_errors: str, src_decode_errors: str = "",
            out_audio_errors: str = "") -> Verdict:
    """`decode_errors`: the output's full decode. `out_audio_errors` and
    `src_decode_errors`: the output's and the source's audio-only decodes. Audio is
    copied as-is, so output audio errors the source already has are not the
    encode's. Video errors are never excused."""
    problems = []
    # A trailing subtitle or audio packet can stretch a container's duration by a
    # few seconds beyond the video; allow that much on long files.
    if abs(src.duration - out.duration) > max(1.0, src.duration * 0.0005):
        problems.append(f"duration {src.duration:.1f}s -> {out.duration:.1f}s")
    if out.codec != "hevc":
        problems.append(f"output codec is {out.codec}")
    if not _streams_match(output_streams(src.streams), out.streams):
        problems.append(f"streams differ: {src.streams} -> {out.streams}")
    new_errors = unexplained_errors(decode_errors, src_decode_errors, out_audio_errors)
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
# addresses and input stream labels, which differ between source and output. The
# null muxer's "in stream 1: 112 >= 112" timestamps differ too (containers count
# time differently); its stream number is kept, since the audio-only decodes of
# source and output number their streams alike.
_ERROR_NOISE = re.compile(r"@ (?:0x)?[0-9a-fA-F]+|#\d+:\d+|(?<=in stream)( \d+): \d+ >= \d+")
# The stream number as well: the full decode numbers audio after the video.
_STREAM_NOISE = re.compile(r"in stream \d+")


_REPEATED = re.compile(r"^\s*Last message repeated (\d+) times?$")


def _lines(errors: str) -> list[str]:
    """One entry per message. ffmpeg folds a run of identical lines into "Last
    message repeated N times"; runs break differently when video messages are
    interleaved, so unfold them before comparing."""
    lines: list[str] = []
    for line in errors.strip().splitlines():
        if not line.strip():
            continue
        if m := _REPEATED.match(line):
            if lines:
                lines.extend([lines[-1]] * int(m.group(1)))
        else:
            lines.append(line)
    return lines


def _key(line: str) -> str:
    return _ERROR_NOISE.sub(r"\1", line)


def _take(line: str, pool: Counter, key) -> bool:
    """Consume one matching entry of `pool` for `line`, if there is one."""
    k = key(line)
    if pool[k] <= 0:
        return False
    pool[k] -= 1
    return True


def unexplained_errors(errors: str, src_errors: str, out_audio_errors: str = "") -> list[str]:
    """The lines of `errors` (the output's full decode) the source doesn't account
    for. Lines also seen in `out_audio_errors` (the output's audio-only decode) are
    the audio's; each is excused by one matching line of `src_errors` (the source's
    audio-only decode). Every other line is the video's and is never excused, nor is
    a bare "ffmpeg exit N" (no message to match)."""
    audio = _lines(out_audio_errors)
    audio_left = Counter(_STREAM_NOISE.sub("", _key(line)) for line in audio)
    video = [line for line in _lines(errors)
             if not _take(line, audio_left, lambda l: _STREAM_NOISE.sub("", _key(l)))]
    known = Counter(_key(line) for line in _lines(src_errors)
                    if not line.startswith("ffmpeg exit"))
    return video + [line for line in audio if not _take(line, known, _key)]


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


def audio_errors(src: Path, out: Path) -> tuple[str, str]:
    """The output's and the source's audio decode errors. The source is read only
    when the output's audio has some (decoding the whole source would cost far more)."""
    out_err = decode_errors(out, audio_only=True)
    return out_err, (decode_errors(src, audio_only=True) if out_err else "")


def source_errors(src: Path, out: Path) -> str:
    """The source's audio decode errors, if the output's audio has some."""
    return audio_errors(src, out)[1]
