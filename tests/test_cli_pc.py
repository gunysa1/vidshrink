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
