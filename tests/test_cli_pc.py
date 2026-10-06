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
