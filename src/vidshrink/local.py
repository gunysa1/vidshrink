"""Compress a local folder of videos in place, outside the Drive library.

Each video is encoded to HEVC next to itself as `<stem>.vidshrink-tmp.mkv`, checked
exactly as library encodes are (duration, streams, decode errors, at least
min_saving smaller), and only then replaces the original: the original moves into
`_vidshrink_originals/` in the same folder (delete that folder once you're happy)
and the encode takes the name `<stem>.mkv`. A file that fails a check, or wouldn't
save enough, is left exactly as it was. Must stay importable on Windows (no
vidshrink.worker, no fcntl)."""
from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vidshrink.encode import EncodeCancelled, EncodeError
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.scorer import EFFICIENT, INTERLACED, codec_family
from vidshrink.verify import compare, decode_errors

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".mts",
              ".webm", ".flv", ".mpg", ".mpeg", ".3gp"}
ORIGINALS = "_vidshrink_originals"
TMP_SUFFIX = ".vidshrink-tmp.mkv"
LOG_NAME = "_vidshrink_local.log"

# (src, out, probe, log_path, cancel) -> encode seconds, as in pc_worker
EncodeFn = Callable[[Path, Path, Probe, Path, threading.Event], float]


@dataclass
class Totals:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    saved: int = 0  # bytes


def skip_reason(probe: Probe) -> str | None:
    """Why a local video isn't worth encoding (the library's rules, minus the
    size and bitrate floors: the min_saving check covers those here)."""
    if probe.codec in EFFICIENT:
        return f"already efficient ({probe.codec})"
    if probe.is_dv:
        return "dolby vision"
    if probe.field_order in INTERLACED:
        return "interlaced"
    if codec_family(probe.codec) is None:
        return f"unsupported codec ({probe.codec})"
    return None


def candidates(folder: Path) -> list[Path]:
    """Videos directly in `folder`, biggest first; never our own tmp files."""
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXTS
             and not p.name.endswith(TMP_SUFFIX)]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)


def compress_folder(folder: Path, encode: EncodeFn, min_saving: float = 0.20,
                    dry_run: bool = False, limit: int | None = None,
                    cancel: threading.Event | None = None,
                    log: Callable[[str], None] = print) -> Totals:
    folder = Path(folder)
    cancel = cancel or threading.Event()
    totals = Totals()
    logfile = folder / LOG_NAME

    def note(msg: str) -> None:
        log(msg)
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass

    for stale in folder.glob(f"*{TMP_SUFFIX}"):  # an encode interrupted last time
        stale.unlink(missing_ok=True)
    for src in candidates(folder):
        if cancel.is_set() or (limit is not None and totals.done >= limit):
            break
        size = src.stat().st_size
        try:
            probe = parse_ffprobe(run_ffprobe(str(src)), size)
        except ProbeError as e:
            totals.skipped += 1
            note(f"skip (unreadable): {src.name}: {str(e)[:120]}")
            continue
        why = skip_reason(probe)
        if why:
            totals.skipped += 1
            note(f"skip ({why}): {src.name}")
            continue
        final = src.with_suffix(".mkv")
        if final != src and final.exists():
            totals.skipped += 1
            note(f"skip ({final.name} already exists): {src.name}")
            continue
        if dry_run:
            note(f"would encode: {src.name} ({size / 1e9:.2f} GB, {probe.codec} "
                 f"{probe.width}x{probe.height})")
            continue
        tmp = folder / (src.stem + TMP_SUFFIX)
        enc_log = folder / (src.stem + ".vidshrink-encode.log")
        note(f"encoding: {src.name} ({size / 1e9:.2f} GB)")
        try:
            secs = encode(src, tmp, probe, enc_log, cancel)
            out_size = tmp.stat().st_size
            verdict = compare(probe, parse_ffprobe(run_ffprobe(str(tmp)), out_size),
                              size, out_size, min_saving, decode_errors(tmp))
        except EncodeCancelled:
            tmp.unlink(missing_ok=True)
            note(f"stopped during: {src.name} (left unchanged)")
            break
        except (EncodeError, ProbeError, OSError) as e:
            tmp.unlink(missing_ok=True)
            totals.failed += 1
            note(f"failed: {src.name}: {str(e)[:200]}")
            continue
        if not verdict.ok:
            tmp.unlink(missing_ok=True)
            reason = ("; ".join(verdict.problems) if verdict.problems
                      else f"only {1 - out_size / size:.0%} smaller")
            if verdict.problems:
                totals.failed += 1
            else:
                totals.skipped += 1
            note(f"kept original ({reason}): {src.name}")
            continue
        keep = folder / ORIGINALS
        keep.mkdir(exist_ok=True)
        dest = keep / src.name
        if dest.exists():
            tmp.unlink(missing_ok=True)
            totals.skipped += 1
            note(f"skip ({ORIGINALS}/{src.name} already exists): {src.name}")
            continue
        shutil.move(str(src), str(dest))
        tmp.replace(final)
        enc_log.unlink(missing_ok=True)
        totals.done += 1
        totals.saved += size - out_size
        note(f"done: {src.name} -> {final.name}: {size / 1e9:.2f} -> "
             f"{out_size / 1e9:.2f} GB ({1 - out_size / size:.0%} smaller, "
             f"{secs / 60:.0f} min)")
    note(f"finished: {totals.done} compressed, {totals.saved / 1e9:.1f} GB saved, "
         f"{totals.skipped} skipped, {totals.failed} failed")
    return totals
