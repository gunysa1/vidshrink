import importlib
import sys


def test_cli_imports_without_fcntl(monkeypatch):
    """The PC is Windows (no fcntl): `vidshrink pc-worker/tray/calibrate` must load."""
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ImportError
    for name in [m for m in sys.modules if m.startswith("vidshrink")]:
        monkeypatch.delitem(sys.modules, name)
    cli = importlib.import_module("vidshrink.cli")
    assert callable(cli.main)
    for mod in ("vidshrink.pc_worker", "vidshrink.calibrate", "vidshrink.tray", "vidshrink.jobs"):
        importlib.import_module(mod)
    assert "vidshrink.worker" not in sys.modules
