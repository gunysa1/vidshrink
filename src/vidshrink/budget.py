import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from vidshrink import db

PACIFIC = ZoneInfo("America/Los_Angeles")


class Budget:
    """Bytes per Pacific day against a limit, for uploads or downloads (kept apart)."""

    def __init__(self, conn, limit: int | None, now: Callable[[], float] = time.time,
                 kind: str = "upload"):
        self.conn = conn
        self.limit = limit
        self.now = now
        self.kind = kind
        self._prefix = "" if kind == "upload" else f"{kind}:"
        self._block_key = "blocked_until" if kind == "upload" else f"{kind}_blocked_until"

    def day(self) -> str:
        return datetime.fromtimestamp(self.now(), PACIFIC).date().isoformat()

    def used(self) -> int:
        return db.upload_bytes(self.conn, self._prefix + self.day())

    def add(self, n: int) -> None:
        db.add_upload_bytes(self.conn, self._prefix + self.day(), n)

    def block(self, seconds: float) -> None:
        db.kv_set(self.conn, self._block_key, str(self.now() + seconds))

    def _blocked_until(self) -> float:
        return float(db.kv_get(self.conn, self._block_key, "0"))

    def blocked(self) -> bool:
        return self.now() < self._blocked_until()

    def allows(self, n: int) -> bool:
        if self.now() < self._blocked_until():
            return False
        used = self.used()
        return self.limit is None or used == 0 or used + n <= self.limit

    def seconds_until_reset(self) -> float:
        now = self.now()
        blocked = self._blocked_until()
        if blocked > now:
            return blocked - now
        today = datetime.fromtimestamp(now, PACIFIC).date()
        midnight = datetime.combine(today + timedelta(days=1), dtime(0), PACIFIC)
        return max(midnight.timestamp() - now, 1.0)
