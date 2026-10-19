import threading
import time

from vidshrink.tray import TrayController, tick, tray_title


class FakeWorker:
    def __init__(self):
        self.state = "idle"
        self.stage = ""
        self.current_path = None
        self.done = 0
        self.download_today = None
        self._stop = threading.Event()
        self.now = None

    def status_line(self):
        return f"Encoding {self.current_path.rsplit('/', 1)[-1]}" if self.current_path else ""

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
    assert "Encoding x.mkv" in c.status_text()
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


def test_tray_title_fits_windows_limit():
    c = TrayController(FakeWorker)
    c.start()
    assert wait(lambda: c.state == "working")
    long_line = " · ".join(f"Uploading {'x' * 60}{i}.mkv" for i in range(4))
    c.worker.status_line = lambda: long_line
    c.worker.download_today = (412e9, None)
    title = tray_title("vidshrink", c)
    assert len(title) <= 127
    assert title.endswith(" · DL today 412 GB")  # the short form trims the stages
    assert long_line in c.status_text()  # the menu status item keeps it all
    c.stop_now()


def test_tray_shows_status_line():
    c = TrayController(FakeWorker)
    c.start()
    assert wait(lambda: c.state == "working")
    line = "Encoding X.mkv (2 waiting) · Downloading Z.mkv · Uploading Y.mkv"
    c.worker.status_line = lambda: line
    c.worker.done = 3
    assert c.status_text() == f"Working: {line} (3 done)"
    c.worker.status_line = lambda: ""
    assert c.status_text() == "Working: waiting (3 done)"
    c.stop_now()
    assert wait(lambda: c.state == "off")


def test_ticker_survives_refresh_exception():
    logged = []

    def boom(icon):
        raise RuntimeError("tray backend hiccup")

    tick(boom, object(), logged.append)  # must not raise
    assert logged and "tray backend hiccup" in logged[0]


def test_status_text_shows_download_today_when_known():
    c = TrayController(FakeWorker)
    c.start()
    assert wait(lambda: c.state == "working")
    assert "DL today" not in c.status_text()
    c.worker.download_today = (412e9, 750e9)
    assert c.status_text().endswith(" · DL today 412/750 GB")
    c.stop_now()
    assert wait(lambda: c.state == "off")
    assert c.status_text().endswith(" · DL today 412/750 GB")


def test_tray_status_uncapped():
    c = TrayController(FakeWorker)
    c.start()
    assert wait(lambda: c.state == "working")
    c.worker.download_today = (2e11, None)
    text = c.status_text()
    assert text.endswith(" · DL today 200 GB")
    assert "/" not in text.split("·")[-1]
    c.stop_now()
    assert wait(lambda: c.state == "off")


def test_no_dead_session_done_attribute():
    assert not hasattr(TrayController(FakeWorker), "session_done")
