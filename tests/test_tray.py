import threading
import time

from vidshrink.tray import TrayController


class FakeWorker:
    def __init__(self):
        self.state = "idle"
        self.stage = ""
        self.current_path = None
        self.done = 0
        self._stop = threading.Event()
        self.now = None

    def run(self):
        self.state = "working"
        self.current_path = "Movies/x.mkv"
        self.stage = "encoding"
        self._stop.wait(10)
        self.state = "stopped"

    def stop(self, now):
        self.now = now
        if not now:
            self.state = "finishing"
        self._stop.set()


def wait(cond):
    end = time.time() + 5
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_start_stop_now():
    workers = []
    c = TrayController(lambda: workers.append(FakeWorker()) or workers[-1])
    assert c.state == "off"
    c.start()
    assert wait(lambda: c.state == "working")
    assert "Movies/x.mkv" in c.status_text() and "encoding" in c.status_text()
    c.start()  # no second worker
    assert len(workers) == 1
    c.stop_now()
    assert wait(lambda: c.state == "off")
    assert workers[0].now is True


def test_stop_after_shows_finishing():
    c = TrayController(FakeWorker)
    c.start()
    assert wait(lambda: c.state == "working")
    c.stop_after()
    assert c.state == "finishing"
    assert wait(lambda: c.state == "off")


def test_shutdown_when_off_is_safe():
    TrayController(FakeWorker).shutdown()
