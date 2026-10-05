import hashlib
import sqlite3

from vidshrink import db
from vidshrink.paths import final_path, md5_file, tmp_path


def test_paths():
    assert final_path("TV/S1/e.avi") == "TV/S1/e.mkv"
    assert tmp_path("TV/S1/e.avi") == "TV/S1/e.vidshrink-tmp.mkv"


def test_md5_file(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"abc" * 1000)
    assert md5_file(f) == hashlib.md5(b"abc" * 1000).hexdigest()


def test_paths_module_does_not_need_fcntl():
    import vidshrink.paths as p
    assert "fcntl" not in p.__dict__


def test_connect_migrates_old_schema(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # the original columns only
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    assert {"worker", "lease_until", "encoder", "remote_md5"} <= cols
    db.connect(path).close()  # idempotent


def test_upsert_leaves_remote_stages_alone(conn):
    for stage in db.REMOTE_STAGES:
        db.upsert_listing(conn, f"{stage}.mkv", 100, "t1")
        vid = db.get_by_path(conn, f"{stage}.mkv")["id"]
        db.mark(conn, vid, stage)
        db.upsert_listing(conn, f"{stage}.mkv", 999, "t9")
        assert db.get(conn, vid)["stage"] == stage
