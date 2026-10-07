import threading
import time

from vidshrink.cli import main
from vidshrink.jobs import JobClient, load_or_create_token
from vidshrink.rclone import free_port


def test_token_command(cfg, capsys, monkeypatch):
    monkeypatch.setenv("VIDSHRINK_TOKEN_PATH", str(cfg.db_path.parent / "serve.token"))
    assert main(["--db", str(cfg.db_path), "token"]) == 0
    tok = capsys.readouterr().out.strip()
    assert tok == load_or_create_token(cfg.db_path.parent / "serve.token")


def test_serve_command_answers_health(cfg, monkeypatch):
    monkeypatch.setenv("VIDSHRINK_TOKEN_PATH", str(cfg.db_path.parent / "serve.token"))
    port = free_port()
    common = ["--remote", cfg.remote, "--db", str(cfg.db_path), "--workdir", str(cfg.workdir)]
    t = threading.Thread(target=main, args=(common + ["serve", "--host", "127.0.0.1",
                                                      "--port", str(port)],), daemon=True)
    t.start()
    tok = None
    for _ in range(50):
        try:
            tok = load_or_create_token(cfg.db_path.parent / "serve.token")
            if JobClient(f"http://127.0.0.1:{port}", tok, timeout=2).health()["ok"]:
                break
        except Exception:
            time.sleep(0.1)
    assert JobClient(f"http://127.0.0.1:{port}", tok).health() == {"ok": True}


def test_pc_worker_ctrl_c_stops_now_and_joins(tmp_path, monkeypatch):
    import vidshrink.pc_worker as pcw

    conf = tmp_path / "config.toml"
    conf.write_text(f'server = "http://127.0.0.1:1"\ntoken = "t"\n'
                    f'workdir = "{(tmp_path / "w").as_posix()}"\n')
    made = []

    class FakePC:
        def __init__(self, *a, **k):
            self.stopped = threading.Event()
            self.stop_calls = []
            self.finished = False
            made.append(self)

        def run(self):
            self.stopped.wait(30)  # blocks like a real job until stopped
            self.finished = True

        def stop(self, now):
            self.stop_calls.append(now)
            self.stopped.set()

    monkeypatch.setattr(pcw, "PCWorker", FakePC)
    real_join = threading.Thread.join
    joins = []

    def join(self, timeout=None):
        joins.append(timeout)
        if len(joins) == 1:
            raise KeyboardInterrupt  # Ctrl-C lands while the main thread waits
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", join)
    assert main(["pc-worker", "--config", str(conf)]) == 0
    assert made[0].stop_calls == [True]
    assert made[0].finished  # the worker thread ran to the end: joined, not abandoned
    assert joins[-1] is None  # final join waits for it without a timeout


def test_calibrate_holds_leases_until_all_samples_are_cut(tmp_path, monkeypatch, capsys):
    """The server hands out the top-scored queued job; releasing each job right after
    its sample would make the next claim return (and download) the same file again."""
    import argparse
    import vidshrink.calibrate as cal
    import vidshrink.pc_worker as pcw
    from vidshrink.cli import _calibrate
    from vidshrink.pc_worker import PCConfig

    probe = dict(container="matroska", duration=600.0, codec="h264", width=1920,
                 height=1080, video_index=0, vbitrate=8e6, is_dv=False,
                 color_primaries=None, color_transfer=None, color_space=None,
                 master_display=None, max_cll=None, streams=[])

    class TopFirstClient:
        def __init__(self):
            self.queued, self.leased, self.calls = [1, 2, 3, 4], set(), []

        def claim(self, worker, encoder, download_account="main"):
            self.calls.append(("claim", download_account))
            for i in self.queued:  # top-scored first, like the server
                if i not in self.leased:
                    self.leased.add(i)
                    return {"job": {"id": i, "path": f"v{i}.mkv", "probe": probe}}
            return {"job": None}

        def heartbeat(self, job_id, worker, download_account="main"):
            self.calls.append(("hb", job_id, download_account))
            return {"cancel": job_id not in self.leased}

        def release(self, job_id, worker):
            self.calls.append(("rel", job_id))
            self.leased.discard(job_id)

    downloaded = []

    class FakeRc:
        def download(self, path, dest):
            downloaded.append(path)
            dest.write_bytes(b"x")

    client = TopFirstClient()
    monkeypatch.setattr(pcw, "pc_download_rclone", lambda pcfg: FakeRc())
    monkeypatch.setattr(cal, "cut_sample", lambda src, out, p: out.write_bytes(b"s"))
    monkeypatch.setattr(cal, "measure", lambda samples, *a, **k:
                        (client.calls.append("measure"), [])[1])
    monkeypatch.setattr(cal, "format_table", lambda rows: "table")
    monkeypatch.setattr(cal, "pick_cq", lambda rows: 23)
    pcfg = PCConfig(server="http://x", token="t", workdir=tmp_path / "w", worker_name="pc",
                    download_remote="gdrive-pc:")
    args = argparse.Namespace(samples=3, cqs="22,24", config=None)

    assert _calibrate(args, pcfg, client) == 0
    assert downloaded == ["v1.mkv", "v2.mkv", "v3.mkv"]
    assert client.leased == set()  # all released
    assert [c[1] for c in client.calls if c[0] == "claim"] == ["pc", "pc", "pc"]
    assert [c[2] for c in client.calls if c[0] == "hb"] == ["pc"] * (
        len([c for c in client.calls if c[0] == "hb"]))
    assert client.calls.index("measure") > max(
        i for i, c in enumerate(client.calls) if c[0] == "rel")
    assert not list((tmp_path / "w").iterdir())  # samples cleaned up
