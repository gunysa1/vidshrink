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
