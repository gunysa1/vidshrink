import json

from vidshrink.local_queue import LocalQueue
from vidshrink.pc_worker import clean_workdir


def _job(jid, size=100, hours=1.0):
    return {"id": jid, "path": f"a/{jid}.mkv", "size": size, "modtime": "t",
            "final_path": f"a/{jid}.mkv", "tmp_path": f"a/{jid}.tmp",
            "probe": {}, "expected_hours": hours}


def _queue(tmp_path, disk=10_000, hours=24.0, factor=2.0, reserve=0):
    return LocalQueue(tmp_path, disk, hours, factor, reserve)


def _put(tmp_path, jid, size=100, downloaded=True, encoded=False, src=True):
    (tmp_path / f"{jid}.job.json").write_text(json.dumps(_job(jid, size)))
    if src:
        (tmp_path / f"{jid}.src").write_bytes(b"x" * size)
    if downloaded:
        (tmp_path / f"{jid}.downloaded").write_text(str(size))
    if encoded:
        (tmp_path / f"{jid}.out.mkv").write_bytes(b"y" * 10)
        (tmp_path / f"{jid}.encoded").write_text("12.5")


def test_scan_finds_downloaded_and_encoded(tmp_path):
    _put(tmp_path, 1)
    _put(tmp_path, 2, encoded=True)
    q = _queue(tmp_path)
    jobs, incomplete = q.scan()
    assert {j.id: j.state for j in jobs} == {1: "downloaded", 2: "encoded"}
    assert incomplete == []
    assert q.held_ids() == {1, 2}
    assert jobs[0].src == tmp_path / "1.src" and jobs[0].out == tmp_path / "1.out.mkv"
    assert jobs[0].log == tmp_path / "1.log"


def test_scan_reports_partial_download_as_incomplete(tmp_path):
    _put(tmp_path, 3, downloaded=False)
    _put(tmp_path, 4, size=100)
    (tmp_path / "4.src").write_bytes(b"x" * 50)  # marker but wrong size
    q = _queue(tmp_path)
    jobs, incomplete = q.scan()
    assert jobs == [] and sorted(incomplete) == [3, 4]
    assert q.held_ids() == set()


def test_scan_ignores_corrupt_job_json(tmp_path):
    (tmp_path / "5.job.json").write_text("{not json")
    (tmp_path / "5.downloaded").write_text("100")
    jobs, incomplete = _queue(tmp_path).scan()
    assert jobs == [] and incomplete == []


def test_admits_disk_cap(tmp_path):
    q = _queue(tmp_path, disk=1000)
    j = q.add(_job(1))
    (tmp_path / "1.src").write_bytes(b"x" * 600)
    q.set_state(1, "downloaded")
    assert q.admits(400, 1.0, 10**9)
    assert not q.admits(401, 1.0, 10**9)
    (tmp_path / "1.out.mkv").write_bytes(b"y" * 100)
    assert not q.admits(301, 1.0, 10**9)
    assert j.state == "downloaded"


def test_admits_hours_cap_allows_first_job_into_empty_queue(tmp_path):
    q = _queue(tmp_path, hours=24.0)
    assert q.admits(100, 30.0, 10**9)
    q.add(_job(1, hours=30.0))
    assert not q.admits(100, 1.0, 10**9)
    q.set_state(1, "encoded")
    assert q.admits(100, 1.0, 10**9)


def test_admits_respects_real_free_space(tmp_path):
    q = _queue(tmp_path, factor=2.0, reserve=50)
    assert q.admits(100, 1.0, 250)
    assert not q.admits(100, 1.0, 249)


def test_forget_deletes_only_that_id(tmp_path):
    _put(tmp_path, 1, encoded=True)
    _put(tmp_path, 2)
    (tmp_path / "1.log").write_text("l")
    (tmp_path / "1.job.json.tmp").write_text("{}")
    (tmp_path / "notes.txt").write_text("n")
    q = _queue(tmp_path)
    q.scan()
    q.forget(1)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2.downloaded", "2.job.json", "2.src", "notes.txt"]
    assert q.held_ids() == {2}


def test_clean_workdir_keeps_all_held_ids(tmp_path):
    for i in (1, 2, 3):
        _put(tmp_path, i)
    deleted = clean_workdir(tmp_path, {1, 3})
    assert {p.name for p in deleted} == {"2.src", "2.downloaded", "2.job.json"}
    assert (tmp_path / "1.job.json").exists() and (tmp_path / "3.src").exists()
