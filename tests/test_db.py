import sqlite3

import pytest

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


def test_transaction_takes_write_lock_up_front(conn, cfg):
    other = sqlite3.connect(cfg.db_path, timeout=0)
    try:
        with db.transaction(conn):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()
