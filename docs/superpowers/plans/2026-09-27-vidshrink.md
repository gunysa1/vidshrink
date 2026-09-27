# vidshrink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI + systemd service that re-encodes the highest-value videos on the `gdrive:` rclone remote to HEVC and replaces the originals in place after automated checks.

**Architecture:** A Python package (stdlib only at runtime) with one SQLite database as the source of truth. A scanner lists and probes videos, a scorer ranks them by GB saved per encode-hour, and a worker moves one file at a time through download → encode → verify → upload → swap, saving the stage after each step so it can resume after a crash. All Drive access goes through the `rclone` CLI; all media work goes through `ffmpeg`/`ffprobe`.

**Tech Stack:** Python 3.13 (stdlib: sqlite3, subprocess, argparse, zoneinfo, concurrent.futures), pytest, rclone 1.60, ffmpeg 7.1 (libx265), systemd user unit, `uv` for the venv.

**Spec:** `docs/superpowers/specs/2026-09-27-vidshrink-design.md`

## Global Constraints

- Runtime dependencies: Python stdlib only. Dev dependency: `pytest>=8`.
- Python `>=3.11`; package source lives in `src/vidshrink/`, tests in `tests/`.
- Encoder: `libx265`, `-preset medium`, CRF 22 (CRF 24 for 4K), `yuv420p10le`, output container `.mkv`.
- Audio, subtitles, chapters and attachments are copied, never re-encoded. Only exception: `mov_text` subtitles (MP4) become `srt`, because Matroska cannot hold `mov_text`.
- Skip rules: HEVC/AV1; Dolby Vision; size < 200 MB or > 120 GB; bitrate floors SD 0.8, 720p 1.2, 1080p 2.5, 4K 8 Mbps; excluded prefixes `Torrent/`, `Private/`.
- Starting ratios: H.264 0.55, legacy codecs 0.70. Starting speed factors (encode s per video s): SD 0.5, 720p 1.0, 1080p 1.7, 4K 6.5.
- Output must be at least 20% smaller than the source, otherwise it's `not_worth_it`.
- Upload budget 700 GB per Pacific-time day. A Drive rate-limit error blocks uploads for 6 h.
- Retries: 5 attempts with exponential backoff for rclone calls. A file that fails twice is `failed`.
- Disk: needs free space ≥ 2.2 × source size + 20 GB before downloading.
- Temp name on Drive: `<stem>.vidshrink-tmp.mkv`; final name `<stem>.mkv`.
- Tests never touch `gdrive:`. They use rclone against a local temp directory (a plain path works as an rclone remote).
- Every commit message ends with the attribution trailer lines from the session's system reminder.
- Run tests with `.venv/bin/pytest` from `~/vidshrink`.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, `vidshrink` entry point, pytest config |
| `src/vidshrink/config.py` | `Config` dataclass with every tunable default |
| `src/vidshrink/db.py` | SQLite schema and all row-level helpers |
| `src/vidshrink/probe.py` | `Probe` dataclass, ffprobe JSON parsing, running ffprobe |
| `src/vidshrink/scorer.py` | Skip rules, scores, learned averages, `rescore` |
| `src/vidshrink/budget.py` | Daily upload budget in Pacific time, rate-limit block |
| `src/vidshrink/rclone.py` | rclone CLI wrapper, retries, `serve_http` |
| `src/vidshrink/encode.py` | ffmpeg command builder (incl. HDR10 params), running the encode |
| `src/vidshrink/verify.py` | Pre-swap checks |
| `src/vidshrink/scanner.py` | Listing + parallel probing into the DB |
| `src/vidshrink/worker.py` | The per-file stage machine, swap, crash recovery, lock |
| `src/vidshrink/cli.py` | `scan`, `run`, `status` commands |
| `deploy/vidshrink.service` | systemd user unit |
| `tests/conftest.py` | Shared fixtures: config, DB, local "drive", clip factory |

## Stages

`unprobed → probed → queued | skipped | probe_failed | gone`
`queued → downloading → encoding → verifying → ready → uploading → swapping → done`
Also `failed`, `not_worth_it`, `dry_run`.
`ready` means "encoded and checked, waiting for upload budget".

---

### Task 1: Scaffold, config and database

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/vidshrink/__init__.py`, `src/vidshrink/config.py`, `src/vidshrink/db.py`
- Test: `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Produces:
  - `Config` (dataclass, fields listed in code below)
  - `db.connect(path: Path | str) -> sqlite3.Connection` (autocommit, `sqlite3.Row` rows)
  - `db.transaction(conn)` context manager
  - `db.upsert_listing(conn, path: str, size: int, modtime: str) -> None`
  - `db.mark_gone(conn, seen: Iterable[str]) -> int`
  - `db.save_probe(conn, path: str, probe_json: str) -> None`
  - `db.mark(conn, vid: int, stage: str, **fields) -> None`
  - `db.get(conn, vid: int) -> sqlite3.Row | None`, `db.get_by_path(conn, path: str) -> sqlite3.Row | None`
  - `db.rows_in(conn, *stages: str) -> list[sqlite3.Row]` (highest score first)
  - `db.next_queued(conn) -> sqlite3.Row | None`
  - `db.learn(conn, key: str, value: float)`, `db.learned_mean(conn, key: str, default: float) -> float`
  - `db.kv_get(conn, key: str, default: str | None = None) -> str | None`, `db.kv_set(conn, key: str, value: str)`
  - `db.upload_bytes(conn, day: str) -> int`, `db.add_upload_bytes(conn, day: str, n: int)`
  - Constant `db.IN_PROGRESS = ("downloading", "encoding", "verifying", "uploading", "swapping")`

- [ ] **Step 1: Install system tools and create the venv**

These need the user's go-ahead (installing ffmpeg is rollout step 1 in the spec). Ask before running.

```bash
sudo apt-get install -y ffmpeg
ffmpeg -hide_banner -encoders | grep libx265   # expect a "libx265" line
```

- [ ] **Step 2: Write the scaffold files**

`pyproject.toml`:
```toml
[project]
name = "vidshrink"
version = "0.1.0"
description = "Re-encode Google Drive videos to HEVC to save space"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
vidshrink = "vidshrink.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
state.db*
work/
```

`src/vidshrink/__init__.py`:
```python
"""vidshrink: re-encode Google Drive videos to HEVC."""
```

`src/vidshrink/config.py`:
```python
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / "vidshrink"


@dataclass
class Config:
    remote: str = "gdrive:"
    db_path: Path = HOME / "state.db"
    workdir: Path = HOME / "work"
    excluded: tuple[str, ...] = ("Torrent/", "Private/")
    extensions: tuple[str, ...] = (
        "mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "ts", "m2ts",
    )
    min_size: int = 200_000_000
    max_size: int = 120_000_000_000
    crf: int = 22
    crf_4k: int = 24
    preset: str = "medium"
    min_saving: float = 0.20
    daily_upload_limit: int = 700_000_000_000
    rate_limit_block_secs: float = 6 * 3600
    max_ready: int = 2
    max_attempts: int = 2
    retry_attempts: int = 5
    retry_base_delay: float = 5.0
    disk_factor: float = 2.2
    disk_reserve: int = 20_000_000_000
    disk_wait_secs: float = 600
    rclone_args: tuple[str, ...] = ("--drive-chunk-size", "64M")
    probe_workers: int = 8
```

Create the venv:
```bash
cd ~/vidshrink && uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
```

- [ ] **Step 3: Write the failing DB tests**

`tests/conftest.py`:
```python
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
```

`tests/test_db.py`:
```python
from vidshrink import db


def test_upsert_inserts_unprobed(conn):
    db.upsert_listing(conn, "Movies/a.mkv", 100, "2020-01-01T00:00:00Z")
    row = db.get_by_path(conn, "Movies/a.mkv")
    assert row["stage"] == "unprobed"
    assert row["size"] == 100


def test_upsert_changed_file_resets_probe(conn):
    db.upsert_listing(conn, "a.mkv", 100, "t1")
    db.save_probe(conn, "a.mkv", '{"x": 1}')
    db.upsert_listing(conn, "a.mkv", 200, "t2")
    row = db.get_by_path(conn, "a.mkv")
    assert (row["stage"], row["size"], row["probe_json"]) == ("unprobed", 200, None)


def test_upsert_unchanged_file_keeps_probe(conn):
    db.upsert_listing(conn, "a.mkv", 100, "t1")
    db.save_probe(conn, "a.mkv", '{"x": 1}')
    db.upsert_listing(conn, "a.mkv", 100, "t1")
    assert db.get_by_path(conn, "a.mkv")["stage"] == "probed"


def test_upsert_leaves_done_file_alone(conn):
    db.upsert_listing(conn, "a.mkv", 100, "t1")
    vid = db.get_by_path(conn, "a.mkv")["id"]
    db.mark(conn, vid, "done", out_size=40)
    db.upsert_listing(conn, "a.mkv", 40, "t2")
    assert db.get(conn, vid)["stage"] == "done"


def test_upsert_leaves_in_progress_alone(conn):
    db.upsert_listing(conn, "a.mkv", 100, "t1")
    vid = db.get_by_path(conn, "a.mkv")["id"]
    db.mark(conn, vid, "encoding")
    db.upsert_listing(conn, "a.mkv", 999, "t9")
    assert db.get(conn, vid)["stage"] == "encoding"


def test_mark_gone(conn):
    db.upsert_listing(conn, "a.mkv", 1, "t")
    db.upsert_listing(conn, "b.mkv", 1, "t")
    assert db.mark_gone(conn, ["a.mkv"]) == 1
    assert db.get_by_path(conn, "b.mkv")["stage"] == "gone"
    assert db.get_by_path(conn, "a.mkv")["stage"] == "unprobed"


def test_rows_in_orders_by_score(conn):
    for name, score in [("a", 1.0), ("b", 5.0), ("c", 3.0)]:
        db.upsert_listing(conn, name, 1, "t")
        db.mark(conn, db.get_by_path(conn, name)["id"], "queued", score=score)
    assert [r["path"] for r in db.rows_in(conn, "queued")] == ["b", "c", "a"]
    assert db.next_queued(conn)["path"] == "b"


def test_learned_mean_blends_default(conn):
    assert db.learned_mean(conn, "ratio:h264", 0.5) == 0.5
    db.learn(conn, "ratio:h264", 0.7)
    assert abs(db.learned_mean(conn, "ratio:h264", 0.5) - 0.6) < 1e-9


def test_kv_and_uploads(conn):
    assert db.kv_get(conn, "k") is None
    db.kv_set(conn, "k", "v")
    db.kv_set(conn, "k", "w")
    assert db.kv_get(conn, "k") == "w"
    db.add_upload_bytes(conn, "2026-09-27", 10)
    db.add_upload_bytes(conn, "2026-09-27", 5)
    assert db.upload_bytes(conn, "2026-09-27") == 15
    assert db.upload_bytes(conn, "2026-09-28") == 0


def test_transaction_rolls_back(conn):
    try:
        with db.transaction(conn):
            db.upsert_listing(conn, "a.mkv", 1, "t")
            raise RuntimeError
    except RuntimeError:
        pass
    assert db.get_by_path(conn, "a.mkv") is None
```

- [ ] **Step 4: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: collection error, `cannot import name 'db'`.

- [ ] **Step 5: Implement `src/vidshrink/db.py`**

```python
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

IN_PROGRESS = ("downloading", "encoding", "verifying", "uploading", "swapping")
_GONE_ELIGIBLE = ("unprobed", "probed", "queued", "skipped", "probe_failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    modtime TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'unprobed',
    probe_json TEXT,
    score REAL,
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    out_size INTEGER,
    out_md5 TEXT,
    final_path TEXT,
    encode_secs REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_stage_score ON videos(stage, score DESC);
CREATE TABLE IF NOT EXISTS learned (key TEXT PRIMARY KEY, total REAL NOT NULL, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS uploads (day TEXT PRIMARY KEY, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def upsert_listing(conn, path: str, size: int, modtime: str) -> None:
    row = conn.execute(
        "SELECT size, modtime, stage, out_size FROM videos WHERE path=?", (path,)
    ).fetchone()
    now = time.time()
    if row is None:
        conn.execute(
            "INSERT INTO videos (path, size, modtime, updated_at) VALUES (?, ?, ?, ?)",
            (path, size, modtime, now),
        )
        return
    if row["stage"] == "done" and row["out_size"] == size:
        return
    if row["stage"] in IN_PROGRESS or row["stage"] == "ready":
        return
    if (row["size"], row["modtime"]) != (size, modtime):
        conn.execute(
            "UPDATE videos SET size=?, modtime=?, stage='unprobed', probe_json=NULL,"
            " score=NULL, reason=NULL, attempts=0, updated_at=? WHERE path=?",
            (size, modtime, now, path),
        )


def mark_gone(conn, seen: Iterable[str]) -> int:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM seen")
    conn.executemany("INSERT OR IGNORE INTO seen VALUES (?)", ((p,) for p in seen))
    marks = ",".join("?" * len(_GONE_ELIGIBLE))
    cur = conn.execute(
        f"UPDATE videos SET stage='gone', updated_at=? WHERE stage IN ({marks})"
        " AND path NOT IN (SELECT path FROM seen)",
        (time.time(), *_GONE_ELIGIBLE),
    )
    return cur.rowcount


def save_probe(conn, path: str, probe_json: str) -> None:
    conn.execute(
        "UPDATE videos SET probe_json=?, stage='probed', reason=NULL, updated_at=? WHERE path=?",
        (probe_json, time.time(), path),
    )


def mark(conn, vid: int, stage: str, **fields) -> None:
    fields["stage"] = stage
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), vid))


def get(conn, vid: int):
    return conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()


def get_by_path(conn, path: str):
    return conn.execute("SELECT * FROM videos WHERE path=?", (path,)).fetchone()


def rows_in(conn, *stages: str) -> list:
    marks = ",".join("?" * len(stages))
    return conn.execute(
        f"SELECT * FROM videos WHERE stage IN ({marks})"
        " ORDER BY score IS NULL, score DESC, id",
        stages,
    ).fetchall()


def next_queued(conn):
    return conn.execute(
        "SELECT * FROM videos WHERE stage='queued' ORDER BY score DESC, id LIMIT 1"
    ).fetchone()


def learn(conn, key: str, value: float) -> None:
    conn.execute(
        "INSERT INTO learned (key, total, n) VALUES (?, ?, 1)"
        " ON CONFLICT(key) DO UPDATE SET total=total+excluded.total, n=n+1",
        (key, value),
    )


def learned_mean(conn, key: str, default: float) -> float:
    row = conn.execute("SELECT total, n FROM learned WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return (default + row["total"]) / (1 + row["n"])


def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def kv_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def upload_bytes(conn, day: str) -> int:
    row = conn.execute("SELECT bytes FROM uploads WHERE day=?", (day,)).fetchone()
    return 0 if row is None else row["bytes"]


def add_upload_bytes(conn, day: str, n: int) -> None:
    conn.execute(
        "INSERT INTO uploads (day, bytes) VALUES (?, ?)"
        " ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes",
        (day, n),
    )
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: `10 passed`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "feat: project scaffold, config and SQLite state"
```

---

### Task 2: Probe parsing

**Files:**
- Create: `src/vidshrink/probe.py`
- Modify: `tests/conftest.py` (add the clip factory)
- Test: `tests/test_probe.py`

**Interfaces:**
- Produces:
  - `class ProbeError(Exception)`
  - `@dataclass Probe` with fields `container: str, duration: float, codec: str, width: int, height: int, video_index: int, vbitrate: float, is_dv: bool, color_primaries: str | None, color_transfer: str | None, color_space: str | None, master_display: str | None, max_cll: str | None, streams: list[list[str]]`; property `is_hdr: bool`; `to_json() -> str`; `Probe.from_json(s: str) -> Probe`
  - `streams` is the audio + subtitle streams in file order as `[kind, codec, language]`, e.g. `["audio", "aac", "eng"]`
  - `parse_ffprobe(data: dict, size: int) -> Probe`
  - `run_ffprobe(target: str) -> dict` (target is a local path or an http URL)
  - Test fixture `make_clip(name, *, codec="h264", width=640, height=360, seconds=3, audio_langs=("eng", "heb"), sub_langs=("eng",), container="mkv") -> Path` in `tests/conftest.py`, writing into the fixture's `tmp_path / "clips"`

- [ ] **Step 1: Add the clip factory to `tests/conftest.py`**

Append:
```python
import subprocess
from pathlib import Path

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
```

- [ ] **Step 2: Write the failing probe tests**

`tests/test_probe.py`:
```python
import pytest

from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe

HDR_STREAM = {
    "index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
    "color_primaries": "bt2020", "color_transfer": "smpte2084", "color_space": "bt2020nc",
    "disposition": {"attached_pic": 0},
    "side_data_list": [
        {"side_data_type": "Mastering display metadata",
         "red_x": "34000/50000", "red_y": "16000/50000",
         "green_x": "13250/50000", "green_y": "34500/50000",
         "blue_x": "7500/50000", "blue_y": "3000/50000",
         "white_point_x": "15635/50000", "white_point_y": "16450/50000",
         "min_luminance": "50/10000", "max_luminance": "10000000/10000"},
        {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
    ],
}


def test_parse_hdr_side_data():
    data = {"format": {"format_name": "matroska,webm", "duration": "100.0"},
            "streams": [HDR_STREAM]}
    p = parse_ffprobe(data, size=1_000_000_000)
    assert p.is_hdr and not p.is_dv
    assert p.master_display == "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    assert p.max_cll == "1000,400"


def test_parse_skips_cover_art_and_collects_streams():
    data = {
        "format": {"format_name": "matroska,webm", "duration": "10.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "disposition": {"attached_pic": 0}, "tags": {"BPS": "8000000"}},
            {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
            {"codec_type": "subtitle", "codec_name": "subrip"},
            {"codec_type": "attachment", "codec_name": "ttf"},
        ],
    }
    p = parse_ffprobe(data, size=10_000_000)
    assert (p.codec, p.video_index, p.vbitrate) == ("h264", 1, 8_000_000)
    assert p.streams == [["audio", "ac3", "eng"], ["subtitle", "subrip", "und"]]


def test_parse_estimates_bitrate_from_size():
    data = {"format": {"format_name": "avi", "duration": "8.0"},
            "streams": [{"codec_type": "video", "codec_name": "mpeg4", "width": 640, "height": 480}]}
    p = parse_ffprobe(data, size=1_000_000)
    assert p.vbitrate == pytest.approx(900_000)


def test_parse_detects_dolby_vision():
    stream = dict(HDR_STREAM, side_data_list=[{"side_data_type": "DOVI configuration record"}])
    data = {"format": {"format_name": "matroska,webm", "duration": "1"}, "streams": [stream]}
    assert parse_ffprobe(data, size=1).is_dv


def test_parse_rejects_no_video():
    with pytest.raises(ProbeError):
        parse_ffprobe({"format": {"duration": "1"}, "streams": [{"codec_type": "audio"}]}, 1)


def test_json_round_trip():
    data = {"format": {"format_name": "matroska,webm", "duration": "100.0"}, "streams": [HDR_STREAM]}
    p = parse_ffprobe(data, size=5)
    assert Probe.from_json(p.to_json()) == p


def test_run_ffprobe_on_real_clip(make_clip):
    clip = make_clip("a")
    p = parse_ffprobe(run_ffprobe(str(clip)), clip.stat().st_size)
    assert p.codec == "h264" and (p.width, p.height) == (640, 360)
    assert p.duration == pytest.approx(3.0, abs=0.1)
    assert p.streams == [["audio", "aac", "eng"], ["audio", "aac", "heb"], ["subtitle", "subrip", "eng"]]


def test_run_ffprobe_fails_on_garbage(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(ProbeError):
        run_ffprobe(str(bad))
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_probe.py -q`
Expected: collection error, `No module named 'vidshrink.probe'`.

- [ ] **Step 4: Implement `src/vidshrink/probe.py`**

```python
import json
import subprocess
from dataclasses import asdict, dataclass

HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


class ProbeError(Exception):
    pass


@dataclass
class Probe:
    container: str
    duration: float
    codec: str
    width: int
    height: int
    video_index: int
    vbitrate: float
    is_dv: bool
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    master_display: str | None
    max_cll: str | None
    streams: list[list[str]]

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Probe":
        return cls(**json.loads(s))


def _ratio(v: str) -> float:
    num, _, den = str(v).partition("/")
    d = float(den or 1)
    return float(num) / d if d else 0.0


def _master_display(sd: dict) -> str:
    def xy(prefix):
        return f"{round(_ratio(sd[prefix + '_x']) * 50000)},{round(_ratio(sd[prefix + '_y']) * 50000)}"

    lmax = round(_ratio(sd["max_luminance"]) * 10000)
    lmin = round(_ratio(sd["min_luminance"]) * 10000)
    return f"G({xy('green')})B({xy('blue')})R({xy('red')})WP({xy('white_point')})L({lmax},{lmin})"


def _stream_bitrate(s: dict) -> float | None:
    for v in (s.get("bit_rate"), (s.get("tags") or {}).get("BPS"),
              (s.get("tags") or {}).get("BPS-eng")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except ValueError:
            pass
    return None


def parse_ffprobe(data: dict, size: int) -> Probe:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video"]
    main = next((s for s in videos if not (s.get("disposition") or {}).get("attached_pic")), None)
    if main is None:
        raise ProbeError("no video stream")
    try:
        duration = float(fmt.get("duration") or main.get("duration"))
    except (TypeError, ValueError):
        raise ProbeError("no duration") from None
    if duration <= 0:
        raise ProbeError("zero duration")

    master = cll = None
    is_dv = main.get("codec_tag_string") in ("dvh1", "dvhe", "dav1")
    for s in videos:
        for sd in s.get("side_data_list") or []:
            kind = sd.get("side_data_type", "")
            if "DOVI" in kind:
                is_dv = True
            elif s is main and kind == "Mastering display metadata":
                try:
                    master = _master_display(sd)
                except (KeyError, ValueError):
                    master = None
            elif s is main and kind == "Content light level metadata":
                cll = f"{sd.get('max_content', 0)},{sd.get('max_average', 0)}"

    vbitrate = _stream_bitrate(main) or size * 8 / duration * 0.9
    return Probe(
        container=fmt.get("format_name", ""),
        duration=duration,
        codec=main.get("codec_name", ""),
        width=int(main.get("width") or 0),
        height=int(main.get("height") or 0),
        video_index=videos.index(main),
        vbitrate=vbitrate,
        is_dv=is_dv,
        color_primaries=main.get("color_primaries"),
        color_transfer=main.get("color_transfer"),
        color_space=main.get("color_space"),
        master_display=master,
        max_cll=cll,
        streams=[
            [s["codec_type"], s.get("codec_name", ""), (s.get("tags") or {}).get("language", "und")]
            for s in streams if s.get("codec_type") in ("audio", "subtitle")
        ],
    )


def run_ffprobe(target: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", target]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe timed out") from None
    if res.returncode != 0:
        raise ProbeError(res.stderr.strip()[:300] or f"ffprobe exit {res.returncode}")
    return json.loads(res.stdout)
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_probe.py -q`
Expected: `8 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/vidshrink/probe.py tests/conftest.py tests/test_probe.py
git commit -m "feat: parse ffprobe output into Probe"
```

---

### Task 3: Scorer

**Files:**
- Create: `src/vidshrink/scorer.py`
- Test: `tests/test_scorer.py`

**Interfaces:**
- Consumes: `Config`, `Probe`, `db.rows_in`, `db.mark`, `db.learned_mean`, `db.transaction`
- Produces:
  - `resolution_class(width: int, height: int) -> str` (`"sd" | "720p" | "1080p" | "4k"`)
  - `codec_family(codec: str) -> str | None` (`"h264" | "legacy" | None`)
  - `skip_reason(path: str, size: int, probe: Probe, cfg: Config) -> str | None`
  - `ratio_for(conn, family: str) -> float`, `speed_for(conn, res: str) -> float` (learned, keys `ratio:<family>` and `speed:<res>`)
  - `score(size: int, probe: Probe, ratio: float, speed: float) -> float`
  - `rescore(conn, cfg: Config) -> None` (all `probed`/`queued` rows become `queued` with a score or `skipped` with a reason)

- [ ] **Step 1: Write the failing tests**

`tests/test_scorer.py`:
```python
from dataclasses import replace

from vidshrink import db, scorer
from vidshrink.probe import Probe

BASE = Probe(container="matroska,webm", duration=3600.0, codec="h264", width=1920, height=1080,
             video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
             color_transfer=None, color_space=None, master_display=None, max_cll=None,
             streams=[["audio", "ac3", "eng"]])


def test_resolution_class():
    assert scorer.resolution_class(720, 480) == "sd"
    assert scorer.resolution_class(1280, 720) == "720p"
    assert scorer.resolution_class(1920, 800) == "1080p"
    assert scorer.resolution_class(3840, 1600) == "4k"


def test_skip_reasons(cfg):
    cfg = replace(cfg, min_size=200_000_000)
    ok = 5_000_000_000
    assert scorer.skip_reason("Movies/a.mkv", ok, BASE, cfg) is None
    assert "excluded" in scorer.skip_reason("Torrent/a.mkv", ok, BASE, cfg)
    assert "small" in scorer.skip_reason("a.mkv", 100_000_000, BASE, cfg)
    assert "large" in scorer.skip_reason("a.mkv", 130_000_000_000, BASE, cfg)
    assert "efficient" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="hevc"), cfg)
    assert "efficient" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="av1"), cfg)
    assert "unsupported" in scorer.skip_reason("a.mkv", ok, replace(BASE, codec="vp9"), cfg)
    assert "dolby" in scorer.skip_reason("a.mkv", ok, replace(BASE, is_dv=True), cfg)
    assert "bitrate" in scorer.skip_reason("a.mkv", ok, replace(BASE, vbitrate=2e6), cfg)


def test_score_units():
    # 10 GB * 0.5 saved = 5 GB; 1 h of video * 2.0 speed = 2 h -> 2.5 GB/h
    assert scorer.score(10_000_000_000, BASE, ratio=0.5, speed=2.0) == 2.5


def _add(conn, path, size, probe):
    db.upsert_listing(conn, path, size, "t")
    db.save_probe(conn, path, probe.to_json())


def test_rescore_orders_legacy_above_h264(conn, cfg):
    _add(conn, "h264.mkv", 5_000_000_000, BASE)
    _add(conn, "xvid.avi", 5_000_000_000, replace(BASE, codec="mpeg4"))
    _add(conn, "hevc.mkv", 5_000_000_000, replace(BASE, codec="hevc"))
    scorer.rescore(conn, cfg)
    assert [r["path"] for r in db.rows_in(conn, "queued")] == ["xvid.avi", "h264.mkv"]
    assert db.get_by_path(conn, "hevc.mkv")["stage"] == "skipped"


def test_rescore_uses_learned_values(conn, cfg):
    _add(conn, "a.mkv", 5_000_000_000, BASE)
    scorer.rescore(conn, cfg)
    before = db.get_by_path(conn, "a.mkv")["score"]
    db.learn(conn, "speed:1080p", 0.1)  # encodes much faster than guessed
    scorer.rescore(conn, cfg)
    assert db.get_by_path(conn, "a.mkv")["score"] > before
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_scorer.py -q`
Expected: collection error, `cannot import name 'scorer'`.

- [ ] **Step 3: Implement `src/vidshrink/scorer.py`**

```python
from vidshrink import db
from vidshrink.config import Config
from vidshrink.probe import Probe

EFFICIENT = {"hevc", "av1"}
H264 = {"h264"}
LEGACY = {"mpeg1video", "mpeg2video", "vc1", "mpeg4", "msmpeg4v1", "msmpeg4v2",
          "msmpeg4v3", "wmv1", "wmv2", "wmv3"}
DEFAULT_RATIO = {"h264": 0.55, "legacy": 0.70}
DEFAULT_SPEED = {"sd": 0.5, "720p": 1.0, "1080p": 1.7, "4k": 6.5}
BITRATE_FLOOR = {"sd": 0.8e6, "720p": 1.2e6, "1080p": 2.5e6, "4k": 8e6}


def resolution_class(width: int, height: int) -> str:
    if width >= 3000 or height >= 1800:
        return "4k"
    if width >= 1700 or height >= 1000:
        return "1080p"
    if width >= 1100 or height >= 650:
        return "720p"
    return "sd"


def codec_family(codec: str) -> str | None:
    if codec in H264:
        return "h264"
    if codec in LEGACY:
        return "legacy"
    return None


def skip_reason(path: str, size: int, probe: Probe, cfg: Config) -> str | None:
    if any(path.startswith(p) for p in cfg.excluded):
        return "excluded folder"
    if size < cfg.min_size:
        return "too small"
    if size > cfg.max_size:
        return "too large"
    if probe.codec in EFFICIENT:
        return f"already efficient ({probe.codec})"
    if probe.is_dv:
        return "dolby vision"
    if codec_family(probe.codec) is None:
        return f"unsupported codec ({probe.codec})"
    res = resolution_class(probe.width, probe.height)
    if probe.vbitrate < BITRATE_FLOOR[res]:
        return f"bitrate already low ({probe.vbitrate / 1e6:.1f} Mbps at {res})"
    return None


def ratio_for(conn, family: str) -> float:
    return db.learned_mean(conn, f"ratio:{family}", DEFAULT_RATIO[family])


def speed_for(conn, res: str) -> float:
    return db.learned_mean(conn, f"speed:{res}", DEFAULT_SPEED[res])


def score(size: int, probe: Probe, ratio: float, speed: float) -> float:
    saved_gb = size / 1e9 * ratio
    hours = probe.duration * speed / 3600
    return saved_gb / max(hours, 1e-6)


def rescore(conn, cfg: Config) -> None:
    ratios = {f: ratio_for(conn, f) for f in DEFAULT_RATIO}
    speeds = {r: speed_for(conn, r) for r in DEFAULT_SPEED}
    rows = db.rows_in(conn, "probed", "queued")
    with db.transaction(conn):
        for row in rows:
            probe = Probe.from_json(row["probe_json"])
            reason = skip_reason(row["path"], row["size"], probe, cfg)
            if reason:
                db.mark(conn, row["id"], "skipped", reason=reason, score=None)
                continue
            family = codec_family(probe.codec)
            res = resolution_class(probe.width, probe.height)
            db.mark(conn, row["id"], "queued",
                    score=score(row["size"], probe, ratios[family], speeds[res]))
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_scorer.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/scorer.py tests/test_scorer.py
git commit -m "feat: skip rules and savings-per-hour scoring"
```

---

### Task 4: Upload budget

**Files:**
- Create: `src/vidshrink/budget.py`
- Test: `tests/test_budget.py`

**Interfaces:**
- Consumes: `db.upload_bytes`, `db.add_upload_bytes`, `db.kv_get`, `db.kv_set`
- Produces:
  - `PACIFIC` (`ZoneInfo("America/Los_Angeles")`)
  - `class Budget(conn, limit: int, now: Callable[[], float] = time.time)` with methods `day() -> str`, `used() -> int`, `add(n: int)`, `block(seconds: float)`, `allows(n: int) -> bool`, `seconds_until_reset() -> float`

- [ ] **Step 1: Write the failing tests**

`tests/test_budget.py`:
```python
from datetime import datetime

from vidshrink.budget import PACIFIC, Budget


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def noon():
    return datetime(2026, 9, 27, 12, 0, tzinfo=PACIFIC).timestamp()


def test_day_is_pacific(conn):
    # 06:00 UTC on the 28th is still the 27th in Pacific time.
    t = datetime.fromisoformat("2026-09-28T06:00:00+00:00").timestamp()
    assert Budget(conn, 100, Clock(t)).day() == "2026-09-27"


def test_allows_until_limit(conn):
    b = Budget(conn, 100, Clock(noon()))
    assert b.allows(100)
    b.add(60)
    assert b.allows(40) and not b.allows(41)


def test_oversized_file_allowed_on_fresh_day(conn):
    assert Budget(conn, 100, Clock(noon())).allows(500)


def test_resets_at_pacific_midnight(conn):
    clock = Clock(noon())
    b = Budget(conn, 100, clock)
    b.add(100)
    assert not b.allows(1)
    assert b.seconds_until_reset() == 12 * 3600
    clock.t += 12 * 3600
    assert b.allows(1)


def test_block_overrides_budget(conn):
    clock = Clock(noon())
    b = Budget(conn, 100, clock)
    b.block(6 * 3600)
    assert not b.allows(1)
    assert b.seconds_until_reset() == 6 * 3600
    clock.t += 6 * 3600
    assert b.allows(1)
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_budget.py -q`
Expected: collection error, `No module named 'vidshrink.budget'`.

- [ ] **Step 3: Implement `src/vidshrink/budget.py`**

```python
import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from vidshrink import db

PACIFIC = ZoneInfo("America/Los_Angeles")


class Budget:
    def __init__(self, conn, limit: int, now: Callable[[], float] = time.time):
        self.conn = conn
        self.limit = limit
        self.now = now

    def day(self) -> str:
        return datetime.fromtimestamp(self.now(), PACIFIC).date().isoformat()

    def used(self) -> int:
        return db.upload_bytes(self.conn, self.day())

    def add(self, n: int) -> None:
        db.add_upload_bytes(self.conn, self.day(), n)

    def block(self, seconds: float) -> None:
        db.kv_set(self.conn, "blocked_until", str(self.now() + seconds))

    def _blocked_until(self) -> float:
        return float(db.kv_get(self.conn, "blocked_until", "0"))

    def allows(self, n: int) -> bool:
        if self.now() < self._blocked_until():
            return False
        used = self.used()
        return used == 0 or used + n <= self.limit

    def seconds_until_reset(self) -> float:
        now = self.now()
        blocked = self._blocked_until()
        if blocked > now:
            return blocked - now
        today = datetime.fromtimestamp(now, PACIFIC).date()
        midnight = datetime.combine(today + timedelta(days=1), dtime(0), PACIFIC)
        return max(midnight.timestamp() - now, 1.0)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_budget.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/budget.py tests/test_budget.py
git commit -m "feat: daily upload budget in Pacific time"
```

---

### Task 5: rclone wrapper

**Files:**
- Create: `src/vidshrink/rclone.py`
- Test: `tests/test_rclone.py`

**Interfaces:**
- Produces:
  - `class RcloneError(Exception)` with attributes `code: int`, `stderr: str` and property `rate_limited: bool`
  - `class Rclone(remote: str, extra_args: Sequence[str] = (), binary: str = "rclone")`. Paths passed to its methods are relative to the remote root (e.g. `"Movies/A/a.mkv"`).
    - `list_videos(extensions: Sequence[str]) -> list[dict]` (rclone lsjson entries with `Path`, `Size`, `ModTime`)
    - `download(path: str, local: Path)`, `upload(local: Path, path: str)`, `delete(path: str)`, `move(src: str, dst: str)`
    - `exists(path: str) -> bool`, `md5(path: str) -> str | None`
  - `with_retries(fn: Callable[[], T], attempts: int, base_delay: float, sleep: Callable[[float], None] = time.sleep) -> T`. Retries `RcloneError` except rate-limit errors, which are re-raised at once.
  - `serve_http(rclone: Rclone, port: int)` context manager yielding a base URL ending in `/`
  - `free_port() -> int`

- [ ] **Step 1: Write the failing tests**

`tests/test_rclone.py`:
```python
import hashlib
import urllib.parse
import urllib.request

import pytest

from vidshrink.rclone import Rclone, RcloneError, free_port, serve_http, with_retries


@pytest.fixture
def drive(tmp_path):
    root = tmp_path / "drive"
    (root / "Movies" / "A [1080p]").mkdir(parents=True)
    (root / "Movies" / "A [1080p]" / "a.mkv").write_bytes(b"x" * 1000)
    (root / "notes.txt").write_text("hi")
    return root


@pytest.fixture
def rc(drive):
    return Rclone(str(drive) + "/")


def test_list_videos(rc):
    entries = rc.list_videos(["mkv", "mp4"])
    assert [(e["Path"], e["Size"]) for e in entries] == [("Movies/A [1080p]/a.mkv", 1000)]


def test_exists_and_md5(rc):
    assert rc.exists("Movies/A [1080p]/a.mkv")
    assert not rc.exists("Movies/A [1080p]/nope.mkv")
    assert not rc.exists("NoDir/a.mkv")
    assert rc.md5("Movies/A [1080p]/a.mkv") == hashlib.md5(b"x" * 1000).hexdigest()


def test_download_upload_move_delete(rc, tmp_path):
    local = tmp_path / "local.bin"
    rc.download("Movies/A [1080p]/a.mkv", local)
    assert local.read_bytes() == b"x" * 1000
    rc.upload(local, "Movies/A [1080p]/b.tmp.mkv")
    rc.move("Movies/A [1080p]/b.tmp.mkv", "Movies/A [1080p]/b.mkv")
    assert rc.exists("Movies/A [1080p]/b.mkv") and not rc.exists("Movies/A [1080p]/b.tmp.mkv")
    rc.delete("Movies/A [1080p]/b.mkv")
    assert not rc.exists("Movies/A [1080p]/b.mkv")


def test_errors_raise(rc, tmp_path):
    with pytest.raises(RcloneError):
        rc.download("missing.mkv", tmp_path / "x")


def test_rate_limited_detection():
    assert RcloneError(1, "googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded").rate_limited
    assert not RcloneError(1, "connection reset").rate_limited


def test_with_retries_retries_then_succeeds():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RcloneError(1, "connection reset")
        return "ok"

    assert with_retries(fn, attempts=5, base_delay=1.0, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_with_retries_gives_up():
    def fn():
        raise RcloneError(1, "boom")

    with pytest.raises(RcloneError):
        with_retries(fn, attempts=3, base_delay=0, sleep=lambda s: None)


def test_with_retries_does_not_retry_rate_limit():
    calls = []

    def fn():
        calls.append(1)
        raise RcloneError(1, "userRateLimitExceeded")

    with pytest.raises(RcloneError):
        with_retries(fn, attempts=5, base_delay=0, sleep=lambda s: None)
    assert len(calls) == 1


def test_serve_http(rc):
    with serve_http(rc, free_port()) as base:
        url = base + urllib.parse.quote("Movies/A [1080p]/a.mkv")
        assert urllib.request.urlopen(url).read() == b"x" * 1000
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_rclone.py -q`
Expected: collection error, `No module named 'vidshrink.rclone'`.

- [ ] **Step 3: Implement `src/vidshrink/rclone.py`**

```python
import json
import posixpath
import random
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
RATE_LIMIT_MARKERS = ("userRateLimitExceeded", "uploadLimitExceeded", "User rate limit exceeded")


class RcloneError(Exception):
    def __init__(self, code: int, stderr: str):
        super().__init__(f"rclone exit {code}: {stderr.strip()[-500:]}")
        self.code = code
        self.stderr = stderr

    @property
    def rate_limited(self) -> bool:
        return any(m in self.stderr for m in RATE_LIMIT_MARKERS)


class Rclone:
    def __init__(self, remote: str, extra_args: Sequence[str] = (), binary: str = "rclone"):
        self.remote = remote
        self.extra_args = list(extra_args)
        self.binary = binary

    def _p(self, path: str) -> str:
        return self.remote + path

    def _run(self, *args: str) -> str:
        res = subprocess.run([self.binary, *args, *self.extra_args],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RcloneError(res.returncode, res.stderr)
        return res.stdout

    def list_videos(self, extensions: Sequence[str]) -> list[dict]:
        pattern = "*.{" + ",".join(extensions) + "}"
        out = self._run("lsjson", "-R", "--files-only", "--no-mimetype",
                        "--ignore-case", "--include", pattern, self.remote)
        return json.loads(out)

    def download(self, path: str, local: Path) -> None:
        self._run("copyto", self._p(path), str(local))

    def upload(self, local: Path, path: str) -> None:
        self._run("copyto", str(local), self._p(path))

    def delete(self, path: str) -> None:
        self._run("deletefile", self._p(path))

    def move(self, src: str, dst: str) -> None:
        self._run("moveto", self._p(src), self._p(dst))

    def exists(self, path: str) -> bool:
        parent, name = posixpath.split(path)
        try:
            out = self._run("lsjson", "--files-only", "--no-mimetype", self._p(parent))
        except RcloneError:
            return False
        return any(e["Name"] == name for e in json.loads(out))

    def md5(self, path: str) -> str | None:
        out = self._run("md5sum", self._p(path)).split()
        return out[0] if out else None


def with_retries(fn: Callable[[], T], attempts: int, base_delay: float,
                 sleep: Callable[[float], None] = time.sleep) -> T:
    for i in range(attempts):
        try:
            return fn()
        except RcloneError as e:
            if e.rate_limited or i == attempts - 1:
                raise
            sleep(base_delay * 2 ** i + random.uniform(0, base_delay))
    raise AssertionError("unreachable")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_http(rclone: Rclone, port: int):
    proc = subprocess.Popen(
        [rclone.binary, "serve", "http", rclone.remote, "--addr", f"127.0.0.1:{port}",
         "--read-only", "--tpslimit", "10", *rclone.extra_args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RcloneError(proc.returncode or 1, "rclone serve http did not start")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_rclone.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/rclone.py tests/test_rclone.py
git commit -m "feat: rclone wrapper with retries and serve http"
```

---

### Task 6: Encoder

**Files:**
- Create: `src/vidshrink/encode.py`
- Test: `tests/test_encode.py`

**Interfaces:**
- Consumes: `Config`, `Probe`, `parse_ffprobe`, `run_ffprobe`, `resolution_class`
- Produces:
  - `class EncodeError(Exception)`
  - `x265_params(probe: Probe) -> str`
  - `build_command(src: Path, out: Path, probe: Probe, cfg: Config) -> list[str]`
  - `run_encode(cmd: list[str], log_path: Path) -> float` (elapsed seconds; raises `EncodeError`)

- [ ] **Step 1: Write the failing tests**

`tests/test_encode.py`:
```python
import json
import subprocess
from dataclasses import replace

import pytest

from vidshrink.encode import EncodeError, build_command, run_encode, x265_params
from vidshrink.probe import Probe, parse_ffprobe, run_ffprobe

SDR = Probe(container="matroska,webm", duration=10.0, codec="h264", width=1920, height=1080,
            video_index=0, vbitrate=10e6, is_dv=False, color_primaries="bt709",
            color_transfer="bt709", color_space="bt709", master_display=None, max_cll=None,
            streams=[])


def test_x265_params_sdr():
    p = x265_params(SDR)
    assert "hdr10" not in p
    assert "colorprim=bt709" in p


def test_x265_params_hdr10():
    hdr = replace(SDR, width=3840, height=2160, color_primaries="bt2020",
                  color_transfer="smpte2084", color_space="bt2020nc",
                  master_display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", max_cll="1000,400")
    p = x265_params(hdr).split(":")
    assert {"hdr10=1", "hdr10-opt=1", "repeat-headers=1", "colorprim=bt2020",
            "transfer=smpte2084", "colormatrix=bt2020nc",
            "master-display=G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", "max-cll=1000,400"} <= set(p)


def test_x265_params_ignores_unknown_colors():
    assert "colorprim" not in x265_params(replace(SDR, color_primaries="unknown"))


def test_command_uses_4k_crf_and_video_index(cfg, tmp_path):
    probe = replace(SDR, width=3840, height=2160, video_index=1)
    cmd = build_command(tmp_path / "s", tmp_path / "o.mkv", probe, cfg)
    assert cmd[cmd.index("-c:v:1") + 1] == "libx265"
    assert cmd[cmd.index("-crf:v:1") + 1] == "24"


def _probe_file(path):
    return parse_ffprobe(run_ffprobe(str(path)), path.stat().st_size)


def test_encode_mkv_keeps_tracks(cfg, make_clip, tmp_path):
    src = make_clip("src")
    out = tmp_path / "out.mkv"
    probe = _probe_file(src)
    secs = run_encode(build_command(src, out, probe, cfg), tmp_path / "log")
    assert secs > 0
    got = _probe_file(out)
    assert got.codec == "hevc"
    assert got.streams == probe.streams
    raw = run_ffprobe(str(out))
    assert raw["streams"][0]["pix_fmt"] == "yuv420p10le"
    assert out.stat().st_size < src.stat().st_size * 0.8


def test_encode_mp4_converts_mov_text(cfg, make_clip, tmp_path):
    src = make_clip("src", container="mp4")
    out = tmp_path / "out.mkv"
    run_encode(build_command(src, out, _probe_file(src), cfg), tmp_path / "log")
    assert _probe_file(out).streams[-1] == ["subtitle", "subrip", "eng"]


def test_encode_failure_raises(cfg, tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 100)
    with pytest.raises(EncodeError):
        run_encode(build_command(bad, tmp_path / "o.mkv", SDR, cfg), tmp_path / "log")
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_encode.py -q`
Expected: collection error, `No module named 'vidshrink.encode'`.

- [ ] **Step 3: Implement `src/vidshrink/encode.py`**

```python
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
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_encode.py -q`
Expected: `7 passed`. If `test_encode_mkv_keeps_tracks` fails on the stream specifier options (`-preset:v:0` etc.), check `tests/../log` output; ffmpeg 7.1 accepts stream specifiers on private encoder options, but if it doesn't, fall back to the unqualified `-preset`, `-crf`, `-x265-params` (they only apply to libx265 anyway) and keep `-c:v:{vi}`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/encode.py tests/test_encode.py
git commit -m "feat: x265 encode command with HDR10 passthrough"
```

---

### Task 7: Pre-swap checks

**Files:**
- Create: `src/vidshrink/verify.py`
- Test: `tests/test_verify.py`

**Interfaces:**
- Consumes: `Probe`
- Produces:
  - `@dataclass Verdict(problems: list[str], not_worth_it: bool)` with property `ok: bool`
  - `compare(src: Probe, out: Probe, src_size: int, out_size: int, min_saving: float, decode_errors: str) -> Verdict`
  - `decode_errors(path: Path) -> str` (empty string means a clean decode)

Note: the spec's check 2 requires matching codecs. The one allowed difference is `mov_text` in the source becoming `subrip` in the output, which Task 6 does on purpose.

- [ ] **Step 1: Write the failing tests**

`tests/test_verify.py`:
```python
from dataclasses import replace

from vidshrink.probe import Probe
from vidshrink.verify import compare, decode_errors

SRC = Probe(container="mp4", duration=100.0, codec="h264", width=1920, height=1080,
            video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
            color_transfer=None, color_space=None, master_display=None, max_cll=None,
            streams=[["audio", "aac", "eng"], ["subtitle", "mov_text", "eng"]])
OUT = replace(SRC, container="matroska,webm", codec="hevc",
              streams=[["audio", "aac", "eng"], ["subtitle", "subrip", "eng"]])


def test_good_output_passes():
    v = compare(SRC, OUT, 1000, 500, 0.2, "")
    assert v.ok and v.problems == [] and not v.not_worth_it


def test_duration_mismatch():
    assert "duration" in compare(SRC, replace(OUT, duration=98.5), 1000, 500, 0.2, "").problems[0]


def test_missing_track():
    v = compare(SRC, replace(OUT, streams=OUT.streams[:1]), 1000, 500, 0.2, "")
    assert "streams" in v.problems[0]


def test_language_change_detected():
    v = compare(SRC, replace(OUT, streams=[["audio", "aac", "heb"], OUT.streams[1]]),
                1000, 500, 0.2, "")
    assert not v.ok


def test_not_hevc():
    assert not compare(SRC, replace(OUT, codec="h264"), 1000, 500, 0.2, "").ok


def test_decode_errors_reported():
    v = compare(SRC, OUT, 1000, 500, 0.2, "Invalid NAL unit\nmore")
    assert v.problems == ["decode errors: Invalid NAL unit"]


def test_not_worth_it():
    v = compare(SRC, OUT, 1000, 850, 0.2, "")
    assert v.not_worth_it and not v.ok and v.problems == []


def test_decode_errors_clean_clip(make_clip):
    assert decode_errors(make_clip("a")) == ""


def test_decode_errors_garbage(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"junk" * 1000)
    assert decode_errors(bad) != ""
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_verify.py -q`
Expected: collection error, `No module named 'vidshrink.verify'`.

- [ ] **Step 3: Implement `src/vidshrink/verify.py`**

```python
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
        capture_output=True, text=True,
    )
    err = res.stderr.strip()
    if res.returncode != 0 and not err:
        err = f"ffmpeg exit {res.returncode}"
    return err
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_verify.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/verify.py tests/test_verify.py
git commit -m "feat: pre-swap output checks"
```

---

### Task 8: Scanner

**Files:**
- Create: `src/vidshrink/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `Rclone.list_videos`, `db.*`, `Probe`, `ProbeError`, `parse_ffprobe`, `run_ffprobe`, `scorer.rescore`
- Produces:
  - `ProbeFn = Callable[[str, int], Probe]` (path relative to remote, size → Probe)
  - `http_prober(base_url: str) -> ProbeFn`
  - `scan(conn, rclone: Rclone, cfg: Config, probe_fn: ProbeFn, log: Callable[[str], None] = print) -> dict[str, int]` with keys `listed`, `gone`, `probed`, `probe_failed`, `excluded`

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner.py`:
```python
import shutil
import urllib.parse

from vidshrink import db
from vidshrink.probe import ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, free_port, serve_http
from vidshrink.scanner import http_prober, scan


def local_prober(root):
    def probe(path, size):
        return parse_ffprobe(run_ffprobe(str(root / path)), size)
    return probe


def setup_drive(cfg, make_clip):
    root = cfg.remote.rstrip("/")
    from pathlib import Path
    root = Path(root)
    (root / "Movies").mkdir()
    (root / "Torrent").mkdir()
    shutil.copy(make_clip("h264"), root / "Movies" / "a.mkv")
    shutil.copy(make_clip("hevc", codec="hevc"), root / "Movies" / "b.mkv")
    shutil.copy(make_clip("t"), root / "Torrent" / "c.mkv")
    (root / "Movies" / "broken.mkv").write_bytes(b"junk" * 100)
    return root


def test_scan_probes_scores_and_skips(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    counts = scan(conn, Rclone(cfg.remote), cfg, local_prober(root), log=lambda s: None)
    assert counts == {"listed": 4, "gone": 0, "probed": 2, "probe_failed": 1, "excluded": 1}
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "queued"
    assert db.get_by_path(conn, "Movies/b.mkv")["stage"] == "skipped"
    assert db.get_by_path(conn, "Torrent/c.mkv")["stage"] == "skipped"
    assert db.get_by_path(conn, "Movies/broken.mkv")["stage"] == "probe_failed"


def test_rescan_only_probes_changed(conn, cfg, make_clip):
    root = setup_drive(cfg, make_clip)
    rc = Rclone(cfg.remote)
    scan(conn, rc, cfg, local_prober(root), log=lambda s: None)
    calls = []

    def counting(path, size):
        calls.append(path)
        raise ProbeError("should not be called for unchanged files")

    (root / "Movies" / "a.mkv").unlink()
    counts = scan(conn, rc, cfg, counting, log=lambda s: None)
    assert counts["gone"] == 1 and calls == []
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "gone"


def test_http_prober(cfg, make_clip):
    from pathlib import Path
    root = Path(cfg.remote.rstrip("/"))
    shutil.copy(make_clip("h264"), root / "a b [x].mkv")
    rc = Rclone(cfg.remote)
    with serve_http(rc, free_port()) as base:
        p = http_prober(base)("a b [x].mkv", (root / "a b [x].mkv").stat().st_size)
    assert p.codec == "h264"
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_scanner.py -q`
Expected: collection error, `No module named 'vidshrink.scanner'`.

- [ ] **Step 3: Implement `src/vidshrink/scanner.py`**

```python
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from vidshrink import db, scorer
from vidshrink.config import Config
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone

ProbeFn = Callable[[str, int], Probe]


def http_prober(base_url: str) -> ProbeFn:
    def probe(path: str, size: int) -> Probe:
        return parse_ffprobe(run_ffprobe(base_url + urllib.parse.quote(path)), size)
    return probe


def scan(conn, rclone: Rclone, cfg: Config, probe_fn: ProbeFn,
         log: Callable[[str], None] = print) -> dict[str, int]:
    entries = rclone.list_videos(cfg.extensions)
    log(f"listed {len(entries)} videos")
    with db.transaction(conn):
        for e in entries:
            db.upsert_listing(conn, e["Path"], e["Size"], e["ModTime"])
        gone = db.mark_gone(conn, (e["Path"] for e in entries))

    counts = {"listed": len(entries), "gone": gone, "probed": 0, "probe_failed": 0, "excluded": 0}
    todo = []
    for row in db.rows_in(conn, "unprobed"):
        if any(row["path"].startswith(p) for p in cfg.excluded):
            db.mark(conn, row["id"], "skipped", reason="excluded folder")
            counts["excluded"] += 1
        else:
            todo.append(row)
    log(f"probing {len(todo)} files")

    with ThreadPoolExecutor(cfg.probe_workers) as pool:
        futures = {pool.submit(probe_fn, r["path"], r["size"]): r for r in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                db.save_probe(conn, row["path"], fut.result().to_json())
                counts["probed"] += 1
            except ProbeError as e:
                db.mark(conn, row["id"], "probe_failed", reason=str(e)[:300])
                counts["probe_failed"] += 1
            if n % 500 == 0:
                log(f"probed {n}/{len(todo)}")

    scorer.rescore(conn, cfg)
    return counts
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_scanner.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/scanner.py tests/test_scanner.py
git commit -m "feat: scanner lists and probes videos into the DB"
```

---

### Task 9: Worker pipeline

**Files:**
- Create: `src/vidshrink/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class AlreadyRunning(Exception)`, `class SwapError(Exception)`
  - `final_path(path: str) -> str` (`<stem>.mkv`), `tmp_path(path: str) -> str` (`<stem>.vidshrink-tmp.mkv`)
  - `class Worker(conn, rclone: Rclone, cfg: Config, budget: Budget, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print)` with:
    - `run(limit: int | None = None, dry_run: bool = False) -> int` (takes the lock, calls `recover()`, returns the number of files that reached `done` or `dry_run`)
    - `prepare(row) -> None` (queued → ready, or failed / not_worth_it / queued for retry)
    - `upload_and_swap(row, dry_run: bool) -> bool`
    - `recover() -> None` (implemented in Task 10; in this task it's a stub that does nothing)

- [ ] **Step 1: Write the failing tests**

`tests/test_worker.py`:
```python
import shutil
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone
from vidshrink.scanner import scan
from vidshrink.worker import AlreadyRunning, Worker, final_path, tmp_path


def test_paths():
    assert final_path("TV/S1/e.avi") == "TV/S1/e.mkv"
    assert final_path("a.mkv") == "a.mkv"
    assert tmp_path("TV/S1/e.avi") == "TV/S1/e.vidshrink-tmp.mkv"


@pytest.fixture
def env(conn, cfg, make_clip):
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    shutil.copy(make_clip("b", container="mp4"), root / "Movies" / "b.mp4")
    rc = Rclone(cfg.remote)

    def prober(path, size):
        return parse_ffprobe(run_ffprobe(str(root / path)), size)

    scan(conn, rc, cfg, prober, log=lambda s: None)
    budget = Budget(conn, cfg.daily_upload_limit)
    worker = Worker(conn, rc, cfg, budget, sleep=lambda s: None, log=lambda s: None)
    return root, worker


def test_run_replaces_files(conn, cfg, env):
    root, worker = env
    before = (root / "Movies" / "a.mkv").stat().st_size
    assert worker.run() == 2
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mkv"]
    assert (root / "Movies" / "a.mkv").stat().st_size < before * 0.8
    out = parse_ffprobe(run_ffprobe(str(root / "Movies" / "b.mkv")), 1)
    assert out.codec == "hevc"
    for path in ("Movies/a.mkv", "Movies/b.mp4"):
        row = db.get_by_path(conn, path)
        assert row["stage"] == "done" and row["out_size"] > 0 and row["encode_secs"] > 0
    assert list(cfg.workdir.glob("*.src")) == [] and list(cfg.workdir.glob("*.mkv")) == []
    assert db.learned_mean(conn, "ratio:h264", 0.55) != 0.55


def test_limit(conn, env):
    _, worker = env
    assert worker.run(limit=1) == 1
    assert len(db.rows_in(conn, "done")) == 1


def test_dry_run_keeps_original(conn, env):
    root, worker = env
    assert worker.run(dry_run=True) == 2
    names = sorted(p.name for p in (root / "Movies").iterdir())
    assert names == ["a.mkv", "a.vidshrink-tmp.mkv", "b.mp4", "b.vidshrink-tmp.mkv"]
    assert len(db.rows_in(conn, "dry_run")) == 2


def test_not_worth_it_leaves_original(conn, cfg, env):
    root, worker = env
    worker.cfg.min_saving = 0.999
    worker.run()
    assert sorted(p.name for p in (root / "Movies").iterdir()) == ["a.mkv", "b.mp4"]
    assert len(db.rows_in(conn, "not_worth_it")) == 2


def test_encode_failure_retries_then_fails(conn, env, monkeypatch):
    root, worker = env
    from vidshrink import worker as worker_mod
    from vidshrink.encode import EncodeError

    def boom(cmd, log_path):
        raise EncodeError("boom")

    monkeypatch.setattr(worker_mod, "run_encode", boom)
    assert worker.run() == 0
    rows = db.rows_in(conn, "failed")
    assert len(rows) == 2 and all(r["attempts"] == 2 and r["reason"] == "boom" for r in rows)


def test_budget_exhausted_buffers_then_waits(conn, env):
    root, worker = env
    worker.budget.add(worker.cfg.daily_upload_limit)  # today is used up
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt  # stop the loop at the first wait

    worker.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert len(db.rows_in(conn, "ready")) == 2  # both encoded, waiting for budget
    assert slept and slept[0] > 0


def test_rate_limit_blocks_uploads(conn, env, monkeypatch):
    root, worker = env
    from vidshrink.rclone import RcloneError

    def limited(local, path):
        raise RcloneError(1, "userRateLimitExceeded")

    monkeypatch.setattr(worker.rclone, "upload", limited)
    worker.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert not worker.budget.allows(1)
    assert db.rows_in(conn, "ready")


def test_lock_prevents_second_worker(env):
    _, worker = env
    from vidshrink.worker import _lock
    with _lock(worker.cfg.workdir):
        with pytest.raises(AlreadyRunning):
            worker.run()


def test_target_name_taken(conn, cfg, env):
    root, worker = env
    (root / "Movies" / "b.mkv").write_bytes(b"someone else's file")
    worker.run()
    row = db.get_by_path(conn, "Movies/b.mp4")
    assert row["stage"] == "failed" and "exists" in row["reason"]
    assert (root / "Movies" / "b.mp4").exists()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_worker.py -q`
Expected: collection error, `No module named 'vidshrink.worker'`.

- [ ] **Step 3: Implement `src/vidshrink/worker.py`**

```python
import fcntl
import hashlib
import posixpath
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.encode import EncodeError, build_command, run_encode
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, with_retries
from vidshrink.verify import compare, decode_errors


class AlreadyRunning(Exception):
    pass


class SwapError(Exception):
    pass


def final_path(path: str) -> str:
    return posixpath.splitext(path)[0] + ".mkv"


def tmp_path(path: str) -> str:
    return posixpath.splitext(path)[0] + ".vidshrink-tmp.mkv"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def _lock(workdir: Path):
    workdir.mkdir(parents=True, exist_ok=True)
    with open(workdir / ".lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(str(workdir)) from None
        yield


class Worker:
    def __init__(self, conn, rclone: Rclone, cfg: Config, budget: Budget,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        self.conn = conn
        self.rclone = rclone
        self.cfg = cfg
        self.budget = budget
        self.sleep = sleep
        self.log = log

    # --- work files -------------------------------------------------------
    def _src(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.src"

    def _out(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.out.mkv"

    def _logfile(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.log"

    def _cleanup(self, vid: int, keep_output: bool = False) -> None:
        self._src(vid).unlink(missing_ok=True)
        self._logfile(vid).unlink(missing_ok=True)
        if not keep_output:
            self._out(vid).unlink(missing_ok=True)

    def _retry(self, fn):
        return with_retries(fn, self.cfg.retry_attempts, self.cfg.retry_base_delay, self.sleep)

    def _fail(self, row, reason: str, retry_stage: str = "queued") -> None:
        attempts = row["attempts"] + 1
        final = attempts >= self.cfg.max_attempts
        self._cleanup(row["id"], keep_output=(retry_stage == "ready" and not final))
        stage = "failed" if final else retry_stage
        db.mark(self.conn, row["id"], stage, reason=reason[:500], attempts=attempts)
        self.log(f"{stage}: {row['path']}: {reason[:200]}")

    # --- main loop ---------------------------------------------------------
    def run(self, limit: int | None = None, dry_run: bool = False) -> int:
        with _lock(self.cfg.workdir):
            self.recover()
            done = 0
            while limit is None or done < limit:
                ready = db.rows_in(self.conn, "ready")
                if ready and self.budget.allows(ready[0]["out_size"]):
                    if self.upload_and_swap(ready[0], dry_run):
                        done += 1
                    continue
                row = db.next_queued(self.conn) if len(ready) < self.cfg.max_ready else None
                if row is not None:
                    self.prepare(row)
                    continue
                if not ready:
                    break
                wait = self.budget.seconds_until_reset()
                self.log(f"upload budget used up; sleeping {wait / 3600:.1f} h")
                self.sleep(wait)
            return done

    def _wait_for_disk(self, size: int) -> None:
        need = self.cfg.disk_factor * size + self.cfg.disk_reserve
        while shutil.disk_usage(self.cfg.workdir).free < need:
            self.log(f"waiting for {need / 1e9:.0f} GB free disk")
            self.sleep(self.cfg.disk_wait_secs)

    def prepare(self, row) -> None:
        vid, path = row["id"], row["path"]
        final = final_path(path)
        probe = Probe.from_json(row["probe_json"])
        src, out = self._src(vid), self._out(vid)
        try:
            if final != path and self.rclone.exists(final):
                db.mark(self.conn, vid, "failed", reason=f"target {final} already exists")
                return
            self._wait_for_disk(row["size"])
            self.log(f"downloading {path} ({row['size'] / 1e9:.1f} GB)")
            db.mark(self.conn, vid, "downloading")
            self._retry(lambda: self.rclone.download(path, src))
            self.log(f"encoding {path}")
            db.mark(self.conn, vid, "encoding")
            secs = run_encode(build_command(src, out, probe, self.cfg), self._logfile(vid))
            db.mark(self.conn, vid, "verifying", encode_secs=secs)
            out_size = out.stat().st_size
            out_probe = parse_ffprobe(run_ffprobe(str(out)), out_size)
            verdict = compare(probe, out_probe, row["size"], out_size,
                              self.cfg.min_saving, decode_errors(out))
        except (RcloneError, EncodeError, ProbeError, OSError) as e:
            self._fail(row, str(e))
            return
        if verdict.problems:
            self._fail(row, "; ".join(verdict.problems))
            return
        if verdict.not_worth_it:
            self._cleanup(vid)
            saved = 1 - out_size / row["size"]
            db.mark(self.conn, vid, "not_worth_it", reason=f"only {saved:.0%} smaller")
            self.log(f"not worth it: {path} ({saved:.0%})")
            return
        md5 = _md5(out)
        self._cleanup(vid, keep_output=True)
        db.mark(self.conn, vid, "ready", out_size=out_size, out_md5=md5, final_path=final)

    def upload_and_swap(self, row, dry_run: bool) -> bool:
        vid, path = row["id"], row["path"]
        tmp = tmp_path(path)
        db.mark(self.conn, vid, "uploading")
        self.log(f"uploading {tmp} ({row['out_size'] / 1e9:.1f} GB)")
        try:
            self._retry(lambda: self.rclone.upload(self._out(vid), tmp))
            remote_md5 = self.rclone.md5(tmp)
        except RcloneError as e:
            if e.rate_limited:
                self.budget.block(self.cfg.rate_limit_block_secs)
                db.mark(self.conn, vid, "ready")
                self.log("Drive upload limit hit; blocking uploads")
                return False
            self._fail(row, f"upload: {e}", retry_stage="ready")
            return False
        self.budget.add(row["out_size"])
        if remote_md5 != row["out_md5"]:
            try:
                self.rclone.delete(tmp)
            except RcloneError:
                pass
            self._fail(row, "checksum mismatch after upload", retry_stage="ready")
            return False
        if dry_run:
            self._cleanup(vid)
            db.mark(self.conn, vid, "dry_run", reason=f"uploaded as {tmp}")
            return True
        db.mark(self.conn, vid, "swapping")
        try:
            self._swap(db.get(self.conn, vid))
        except (SwapError, RcloneError) as e:
            db.mark(self.conn, vid, "failed", reason=f"swap: {e}"[:500])
            self.log(f"SWAP FAILED, needs attention: {path}: {e}")
            return False
        return True

    def _swap(self, row) -> None:
        path, final, tmp = row["path"], row["final_path"], tmp_path(row["path"])
        if self.rclone.exists(tmp):
            if self.rclone.exists(path):
                self._retry(lambda: self.rclone.delete(path))
            self._retry(lambda: self.rclone.move(tmp, final))
        elif not (self.rclone.exists(final) and self.rclone.md5(final) == row["out_md5"]):
            raise SwapError(f"{tmp} missing and {final} does not match the encode")
        self._finish(row)

    def _finish(self, row) -> None:
        self._cleanup(row["id"])
        db.mark(self.conn, row["id"], "done", reason=None)
        probe = Probe.from_json(row["probe_json"])
        family = scorer.codec_family(probe.codec)
        if family:
            db.learn(self.conn, f"ratio:{family}", 1 - row["out_size"] / row["size"])
        if row["encode_secs"]:
            res = scorer.resolution_class(probe.width, probe.height)
            db.learn(self.conn, f"speed:{res}", row["encode_secs"] / probe.duration)
        scorer.rescore(self.conn, self.cfg)
        self.log(f"done: {row['final_path']} saved {(row['size'] - row['out_size']) / 1e9:.1f} GB")

    def recover(self) -> None:
        pass  # Task 10
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_worker.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/worker.py tests/test_worker.py
git commit -m "feat: worker pipeline with checks, budget and swap"
```

---

### Task 10: Crash recovery

**Files:**
- Modify: `src/vidshrink/worker.py` (replace the `recover` stub)
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `Worker._swap`, `Worker._fail`, `Worker._cleanup`, `tmp_path`
- Produces: `Worker.recover() -> None`:
  - `downloading` / `encoding` / `verifying` → work files removed; counts as a failed attempt (`queued`, or `failed` at `max_attempts`)
  - `uploading` → remote temp file deleted if present; `ready` if the local output still exists, else `queued`
  - `swapping` → `_swap` redone from the actual state on Drive; on `SwapError`/`RcloneError` → `failed` with reason `swap: ...`

- [ ] **Step 1: Write the failing tests**

`tests/test_recover.py`:
```python
import hashlib
from pathlib import Path

import pytest

from vidshrink import db
from vidshrink.budget import Budget
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone
from vidshrink.worker import Worker

PROBE = Probe(container="matroska,webm", duration=10.0, codec="h264", width=1920, height=1080,
              video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
              color_transfer=None, color_space=None, master_display=None, max_cll=None,
              streams=[])
NEW = b"new encode"
NEW_MD5 = hashlib.md5(NEW).hexdigest()


@pytest.fixture
def setup(conn, cfg):
    root = Path(cfg.remote.rstrip("/"))
    worker = Worker(conn, Rclone(cfg.remote), cfg, Budget(conn, cfg.daily_upload_limit),
                    sleep=lambda s: None, log=lambda s: None)
    cfg.workdir.mkdir(parents=True, exist_ok=True)

    def row(path, stage, **fields):
        db.upsert_listing(conn, path, 1000, "t")
        db.save_probe(conn, path, PROBE.to_json())
        vid = db.get_by_path(conn, path)["id"]
        db.mark(conn, vid, stage, **fields)
        return vid

    return root, worker, row


SWAP_FIELDS = dict(out_size=len(NEW), out_md5=NEW_MD5, encode_secs=5.0)


def test_swap_both_present(conn, setup):
    root, worker, row = setup
    (root / "e.avi").write_bytes(b"old")
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.avi", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert sorted(p.name for p in root.iterdir()) == ["e.mkv"]
    assert (root / "e.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_original_already_trashed(conn, setup):
    root, worker, row = setup
    (root / "e.vidshrink-tmp.mkv").write_bytes(NEW)
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert (root / "e.mkv").read_bytes() == NEW
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_already_finished(conn, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(NEW)
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    assert db.get(conn, vid)["stage"] == "done"


def test_swap_inconsistent_marks_failed(conn, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(b"old original, never swapped")
    vid = row("e.mkv", "swapping", final_path="e.mkv", **SWAP_FIELDS)
    worker.recover()
    r = db.get(conn, vid)
    assert r["stage"] == "failed" and r["reason"].startswith("swap:")
    assert (root / "e.mkv").read_bytes() == b"old original, never swapped"


def test_interrupted_encode_requeues_then_fails(conn, cfg, setup):
    root, worker, row = setup
    vid = row("e.mkv", "encoding")
    (cfg.workdir / f"{vid}.src").write_bytes(b"partial")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"]) == ("queued", 1)
    assert not (cfg.workdir / f"{vid}.src").exists()
    db.mark(conn, vid, "verifying")
    worker.recover()
    assert db.get(conn, vid)["stage"] == "failed"


def test_interrupted_upload(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.vidshrink-tmp.mkv").write_bytes(b"half")
    vid = row("e.mkv", "uploading", **SWAP_FIELDS)
    (cfg.workdir / f"{vid}.out.mkv").write_bytes(NEW)
    worker.recover()
    assert db.get(conn, vid)["stage"] == "ready"
    assert not (root / "e.vidshrink-tmp.mkv").exists()

    vid2 = row("f.mkv", "uploading", **SWAP_FIELDS)
    worker.recover()
    assert db.get(conn, vid2)["stage"] == "queued"
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest tests/test_recover.py -q`
Expected: 6 failures (stages unchanged, because `recover` is a stub).

- [ ] **Step 3: Replace the stub in `src/vidshrink/worker.py`**

```python
    def recover(self) -> None:
        for row in db.rows_in(self.conn, "downloading", "encoding", "verifying"):
            self._fail(row, f"interrupted during {row['stage']}")
        for row in db.rows_in(self.conn, "uploading"):
            tmp = tmp_path(row["path"])
            try:
                if self.rclone.exists(tmp):
                    self.rclone.delete(tmp)
            except RcloneError as e:
                self.log(f"could not remove {tmp}: {e}")
            stage = "ready" if self._out(row["id"]).exists() else "queued"
            db.mark(self.conn, row["id"], stage)
        for row in db.rows_in(self.conn, "swapping"):
            try:
                self._swap(row)
            except (SwapError, RcloneError) as e:
                db.mark(self.conn, row["id"], "failed", reason=f"swap: {e}"[:500])
                self.log(f"SWAP FAILED, needs attention: {row['path']}: {e}")
```

- [ ] **Step 4: Run all the tests**

Run: `.venv/bin/pytest -q`
Expected: every test passes (`72 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/worker.py tests/test_recover.py
git commit -m "feat: resume or clean up interrupted files on startup"
```

---

### Task 11: CLI, status report and systemd unit

**Files:**
- Create: `src/vidshrink/cli.py`, `deploy/vidshrink.service`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `main(argv: list[str] | None = None) -> int`
  - `status_report(conn, cfg: Config, budget: Budget) -> str`
  - Commands: `vidshrink [--remote R] [--db PATH] [--workdir PATH] scan [--workers N]`, `run [--dry-run] [--limit N]`, `status`

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:
```python
import shutil
from pathlib import Path

from vidshrink import db
from vidshrink.cli import main


def test_scan_run_status_end_to_end(cfg, make_clip, capsys, monkeypatch):
    # Real `scan` path: rclone serve http + ffprobe over HTTP, against a local "drive".
    # The size floor is 200 MB by default, so the CLI test lowers it via the env override.
    root = Path(cfg.remote.rstrip("/"))
    (root / "Movies").mkdir()
    shutil.copy(make_clip("a"), root / "Movies" / "a.mkv")
    monkeypatch.setenv("VIDSHRINK_MIN_SIZE", "0")
    common = ["--remote", cfg.remote, "--db", str(cfg.db_path), "--workdir", str(cfg.workdir)]

    assert main(common + ["scan"]) == 0
    assert main(common + ["status"]) == 0
    out = capsys.readouterr().out
    assert "queued" in out and "Projected" in out

    assert main(common + ["run", "--limit", "1"]) == 0
    conn = db.connect(cfg.db_path)
    assert db.get_by_path(conn, "Movies/a.mkv")["stage"] == "done"
    main(common + ["status"])
    assert "Saved so far" in capsys.readouterr().out
```

- [ ] **Step 2: Run the test to see it fail**

Run: `.venv/bin/pytest tests/test_cli.py -q`
Expected: collection error, `No module named 'vidshrink.cli'`.

- [ ] **Step 3: Implement `src/vidshrink/cli.py`**

```python
import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone, free_port, serve_http
from vidshrink.scanner import http_prober, scan
from vidshrink.worker import Worker


def _log(msg: str) -> None:
    print(msg, flush=True)


def status_report(conn, cfg: Config, budget: Budget) -> str:
    lines = ["Stages:"]
    for row in conn.execute("SELECT stage, COUNT(*) n, SUM(size) s FROM videos GROUP BY stage ORDER BY n DESC"):
        lines.append(f"  {row['stage']:<14} {row['n']:>7}  {(row['s'] or 0) / 1e12:8.2f} TB")

    done = conn.execute("SELECT COUNT(*) n, SUM(size - out_size) s FROM videos WHERE stage='done'").fetchone()
    lines.append(f"Saved so far: {(done['s'] or 0) / 1e12:.2f} TB across {done['n']} files")

    saved = hours = 0.0
    for row in db.rows_in(conn, "queued"):
        p = Probe.from_json(row["probe_json"])
        fam = scorer.codec_family(p.codec)
        res = scorer.resolution_class(p.width, p.height)
        saved += row["size"] * scorer.ratio_for(conn, fam)
        hours += p.duration * scorer.speed_for(conn, res) / 3600
    lines.append(f"Projected for the queue: {saved / 1e12:.2f} TB saved, "
                 f"{hours:,.0f} encode hours ({hours / 24 / 365:.1f} years)")

    lines.append("Top of queue (GB saved per encode hour):")
    for row in db.rows_in(conn, "queued")[:10]:
        lines.append(f"  {row['score']:7.2f}  {row['size'] / 1e9:6.1f} GB  {row['path']}")

    failed = db.rows_in(conn, "failed")
    if failed:
        lines.append(f"Failed ({len(failed)}), latest:")
        for row in sorted(failed, key=lambda r: -r["updated_at"])[:10]:
            lines.append(f"  {row['path']}: {row['reason']}")

    lines.append(f"Uploaded today ({budget.day()} Pacific): "
                 f"{budget.used() / 1e9:.0f} / {budget.limit / 1e9:.0f} GB")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    base = Config()
    ap = argparse.ArgumentParser(prog="vidshrink")
    ap.add_argument("--remote", default=base.remote)
    ap.add_argument("--db", type=Path, default=base.db_path)
    ap.add_argument("--workdir", type=Path, default=base.workdir)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="list and probe videos, then rank them")
    sc.add_argument("--workers", type=int, default=base.probe_workers)
    rn = sub.add_parser("run", help="encode and replace files, best first")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--limit", type=int)
    sub.add_parser("status", help="show progress and the queue")
    args = ap.parse_args(argv)

    cfg = replace(base, remote=args.remote, db_path=args.db, workdir=args.workdir)
    if "VIDSHRINK_MIN_SIZE" in os.environ:
        cfg = replace(cfg, min_size=int(os.environ["VIDSHRINK_MIN_SIZE"]))
    if not cfg.remote.startswith("gdrive:"):
        cfg = replace(cfg, rclone_args=())  # drive-only flags are noise elsewhere

    conn = db.connect(cfg.db_path)
    rclone = Rclone(cfg.remote, cfg.rclone_args)
    budget = Budget(conn, cfg.daily_upload_limit)

    if args.cmd == "scan":
        cfg = replace(cfg, probe_workers=args.workers)
        with serve_http(rclone, free_port()) as url:
            counts = scan(conn, rclone, cfg, http_prober(url), log=_log)
        _log(f"scan finished: {counts}")
    elif args.cmd == "run":
        n = Worker(conn, rclone, cfg, budget, log=_log).run(limit=args.limit, dry_run=args.dry_run)
        _log(f"run finished: {n} files")
    else:
        print(status_report(conn, cfg, budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run all the tests**

Run: `.venv/bin/pytest -q`
Expected: every test passes (`73 passed`). Note: the CLI test uses the default disk check (2.2 × size + 20 GB free), which is fine on this server.

- [ ] **Step 5: Write the systemd unit and README**

`deploy/vidshrink.service`:
```ini
[Unit]
Description=vidshrink: re-encode Google Drive videos to HEVC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/vidshrink/.venv/bin/vidshrink run
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

`README.md`:
````markdown
# vidshrink

Re-encodes the videos on `gdrive:` that save the most space per hour of encoding to HEVC,
and replaces each original in place after automated checks. The original goes to Drive's trash.

Design: `docs/superpowers/specs/2026-09-27-vidshrink-design.md`

## Setup

```bash
sudo apt-get install -y ffmpeg
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
```

## Use

```bash
.venv/bin/vidshrink scan                    # list + probe + rank (read-only on Drive)
.venv/bin/vidshrink status                  # queue, projections, failures, today's upload
.venv/bin/vidshrink run --dry-run --limit 3 # upload *.vidshrink-tmp.mkv next to originals
.venv/bin/vidshrink run --limit 10          # replace 10 files
```

## Service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/vidshrink.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vidshrink
sudo loginctl enable-linger "$USER"         # keep running after logout
journalctl --user -u vidshrink -f
```
````

- [ ] **Step 6: Commit**

```bash
git add src/vidshrink/cli.py tests/test_cli.py deploy README.md
git commit -m "feat: CLI, status report and systemd unit"
```

---

### Task 12: Rollout (every step needs the user's go-ahead)

Nothing in this task is automated. Stop and ask the user before each step, and report the result before moving on.

- [ ] **Step 1: Scan the real Drive (read-only)**

```bash
cd ~/vidshrink && .venv/bin/vidshrink scan 2>&1 | tee scan.log
.venv/bin/vidshrink status
```
Expected: `listed` ≈ 66,276. Show the user the status output: candidate count, projected TB saved and projected encode years. **Decision point:** is the project worth running?

- [ ] **Step 2: Dry run on the top 3**

```bash
.venv/bin/vidshrink run --dry-run --limit 3
```
Expected: three `*.vidshrink-tmp.mkv` files next to their originals on Drive. The user compares quality (e.g. plays both in Plex or VLC). Afterwards, delete the three temp files with `rclone deletefile`, and reset those rows with
`sqlite3 state.db "UPDATE videos SET stage='queued', reason=NULL WHERE stage='dry_run'"`.

- [ ] **Step 3: Live, capped at 10**

```bash
.venv/bin/vidshrink run --limit 10
.venv/bin/vidshrink status
```
Expected: 10 `done` rows and "Saved so far" > 0. The user checks the 10 items in Plex (after a library scan). Record the learned speed factors (`sqlite3 state.db 'SELECT * FROM learned'`).

- [ ] **Step 4: Live without a cap**

Install and start the service as in the README. Check `journalctl --user -u vidshrink` and `vidshrink status` after the first hour.
