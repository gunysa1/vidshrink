"""Tray icon for the PC worker: Start / Stop now / Stop after this file / Status / Exit."""
import atexit
import threading
from collections.abc import Callable

COLORS = {"off": (128, 128, 128), "working": (40, 170, 70), "finishing": (230, 160, 30)}
# Windows rejects a notification-area tooltip longer than this (NOTIFYICONDATA.szTip).
TITLE_MAX = 127


class TrayController:
    def __init__(self, make_worker: Callable):
        self.make_worker = make_worker
        self.worker = None
        self.thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        if self.thread is None or not self.thread.is_alive():
            return "off"
        return "finishing" if self.worker.state == "finishing" else "working"

    def start(self) -> None:
        if self.state != "off":
            return
        self.worker = self.make_worker()
        self.thread = threading.Thread(target=self.worker.run, daemon=True)
        self.thread.start()

    def stop_now(self) -> None:
        if self.worker is not None:
            self.worker.stop(now=True)

    def stop_after(self) -> None:
        if self.worker is not None and self.state != "off":
            self.worker.stop(now=False)

    def shutdown(self) -> None:
        self.stop_now()
        if self.thread is not None:
            self.thread.join(30)

    def status_text(self, short: bool = False, limit: int = TITLE_MAX) -> str:
        """Full status for the menu; short=True trims the stages so the whole text
        fits in `limit` characters, for the size-limited icon tooltip."""
        dl = getattr(self.worker, "download_today", None) if self.worker else None
        tail = ""
        if dl:
            used, cap = dl
            of_limit = "" if cap is None else f"/{cap / 1e9:.0f}"
            tail = f" · DL today {used / 1e9:.0f}{of_limit} GB"
        if self.state == "off":
            done = self.worker.done if self.worker else 0
            return f"Off — {done} file(s) this session" + tail
        w = self.worker
        line = w.status_line() or "waiting"
        head, done = f"{self.state.title()}: ", f" ({w.done} done)"
        if short:
            room = limit - len(head) - len(done) - len(tail)
            if len(line) > room:
                line = line[:max(room - 1, 0)] + "…"
        return head + line + done + tail


def tray_title(title: str, controller: TrayController) -> str:
    prefix = f"{title}: "
    return (prefix + controller.status_text(short=True,
                                            limit=TITLE_MAX - len(prefix)))[:TITLE_MAX]


def tick(refresh: Callable, icon, log: Callable[[str], None] = print) -> None:
    """One ticker step: a failed refresh is logged, never allowed to kill the ticker."""
    try:
        refresh(icon)
    except Exception as e:
        log(f"tray refresh failed: {e!r}")


def _icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color + (255,))
    return img


def run_tray(controller: TrayController, title: str = "vidshrink") -> None:
    import pystray

    def refresh(icon):
        icon.icon = _icon_image(COLORS[controller.state])
        icon.title = tray_title(title, controller)
        icon.update_menu()

    def act(fn):
        def handler(icon, item):
            fn()
            refresh(icon)
        return handler

    stopped = threading.Event()

    def on_exit(icon, item):
        stopped.set()
        controller.shutdown()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: controller.status_text(), None, enabled=False),
        pystray.MenuItem("Start", act(controller.start),
                         enabled=lambda item: controller.state == "off"),
        pystray.MenuItem("Stop now", act(controller.stop_now),
                         enabled=lambda item: controller.state != "off"),
        pystray.MenuItem("Stop after this file", act(controller.stop_after),
                         enabled=lambda item: controller.state == "working"),
        pystray.MenuItem("Exit", on_exit),
    )
    icon = pystray.Icon("vidshrink", _icon_image(COLORS["off"]), title, menu)
    atexit.register(controller.shutdown)  # best effort on logoff; lease expiry covers the rest

    def setup(i):
        i.visible = True

        def ticker():
            while not stopped.wait(5):
                tick(refresh, i)
        threading.Thread(target=ticker, daemon=True).start()

    icon.run(setup=setup)
