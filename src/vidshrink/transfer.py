import threading
from collections.abc import Callable


class Transfer:
    """Runs `fn` (a download or upload) on a daemon thread. `fn` must not touch the
    database: its caller records the outcome on its own thread via result()."""

    def __init__(self, fn: Callable[[], None], cancel: threading.Event | None = None):
        # The event `fn` watches to stop early (rclone's cancel); set by cancel().
        self._cancel = cancel or threading.Event()
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as e:
            self._error = e
        finally:
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until finished or `timeout` passes; True if finished."""
        return self._done.wait(timeout)

    def result(self) -> None:
        """Re-raise `fn`'s exception here, in the caller; None if it succeeded."""
        if not self.done():
            raise RuntimeError("transfer still running")
        if self._error is not None:
            raise self._error

    def cancel(self, timeout: float = 10.0) -> None:
        """Shutdown only: ask `fn` to stop and give it `timeout` to do so, so a child
        rclone doesn't outlive us. Its outcome is never recorded."""
        self._cancel.set()
        self._thread.join(timeout)
