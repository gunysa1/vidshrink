"""Tray icon for the PC worker: Start / Stop now / Stop after this file / Status / Exit."""
import atexit
import threading
from collections.abc import Callable

COLORS = {"off": (128, 128, 128), "working": (40, 170, 70), "finishing": (230, 160, 30)}


class TrayController:
    def __init__(self, make_worker: Callable):
        self.make_worker = make_worker
        self.worker = None
        self.thread: threading.Thread | None = None
        self.session_done = 0

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

    def status_text(self) -> str:
        if self.state == "off":
            done = self.worker.done if self.worker else 0
            return f"Off — {done} file(s) this session"
        w = self.worker
        current = w.current_path if w.current_path else "—"
        return f"{self.state.title()}: {w.stage or 'waiting'} — {current} ({w.done} done)"


def _icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color + (255,))
    return img


def run_tray(controller: TrayController, title: str = "vidshrink") -> None:
    import pystray

    def refresh(icon):
        icon.icon = _icon_image(COLORS[controller.state])
        icon.title = f"{title}: {controller.status_text()}"
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
                refresh(i)
        threading.Thread(target=ticker, daemon=True).start()

    icon.run(setup=setup)
