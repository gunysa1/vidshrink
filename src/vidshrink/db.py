import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

IN_PROGRESS = ("downloading", "encoding", "verifying", "uploading", "swapping")
REMOTE_STAGES = ("leased", "remote_ready")
DUPLICATE_REASON = "duplicate name on Drive"
_GONE_ELIGIBLE = ("unprobed", "probed", "queued", "skipped", "probe_failed")
# Columns added after the first release; connect() adds any that are missing.
MIGRATIONS = (("worker", "TEXT"), ("lease_until", "REAL"), ("encoder", "TEXT"),
              ("remote_md5", "TEXT"), ("reserve_secs", "REAL"))

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
    have = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    for name, kind in MIGRATIONS:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as e:
                # Another process (e.g. serve and run starting together) added it
                # between our table_info read and this ALTER.
                if "duplicate column name" not in str(e):
                    raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    # IMMEDIATE: take the write lock up front so a read-then-write inside the
    # transaction can't interleave with the other process (scan vs run).
    conn.execute("BEGIN IMMEDIATE")
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
    if row["stage"] in IN_PROGRESS or row["stage"] in REMOTE_STAGES or row["stage"] == "ready":
        return
    # A gone row is re-probed even when unchanged: it was restored from the trash or re-listed.
    if row["stage"] == "gone" or (row["size"], row["modtime"]) != (size, modtime):
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


def mark_duplicates(conn, paths: Iterable[str]) -> int:
    """Skip paths that name more than one file on Drive; un-skip ones that no longer do."""
    dups = set(paths)
    now = time.time()
    marks = ",".join("?" * len(_GONE_ELIGIBLE))
    n = 0
    for path in dups:
        n += conn.execute(
            f"UPDATE videos SET stage='skipped', reason=?, score=NULL, updated_at=?"
            f" WHERE path=? AND stage IN ({marks})",
            (DUPLICATE_REASON, now, path, *_GONE_ELIGIBLE),
        ).rowcount
    for row in conn.execute("SELECT id, path FROM videos WHERE stage='skipped' AND reason=?",
                            (DUPLICATE_REASON,)).fetchall():
        if row["path"] not in dups:
            conn.execute("UPDATE videos SET stage='unprobed', reason=NULL, probe_json=NULL,"
                         " updated_at=? WHERE id=?", (now, row["id"]))
    return n


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


def mark_if(conn, vid: int, from_stages: Iterable[str], stage: str, **fields) -> bool:
    """Like mark, but only while the row is still in one of from_stages."""
    from_stages = tuple(from_stages)
    fields["stage"] = stage
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    marks = ",".join("?" * len(from_stages))
    return conn.execute(f"UPDATE videos SET {cols} WHERE id=? AND stage IN ({marks})",
                        (*fields.values(), vid, *from_stages)).rowcount == 1


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


def kv_delete(conn, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key=?", (key,))


def upload_bytes(conn, day: str) -> int:
    row = conn.execute("SELECT bytes FROM uploads WHERE day=?", (day,)).fetchone()
    return 0 if row is None else row["bytes"]


def add_upload_bytes(conn, day: str, n: int) -> None:
    conn.execute(
        "INSERT INTO uploads (day, bytes) VALUES (?, ?)"
        " ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes",
        (day, n),
    )
