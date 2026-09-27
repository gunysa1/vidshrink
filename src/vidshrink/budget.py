import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from vidshrink import db

PACIFIC = ZoneInfo("America/Los_Angeles")


class Budget:
    def __init__(self, conn, limit: int, now: Callable[[], float] = time.time):
        self.conn = conn
        self.limit = limit
        self.now = now

    def day(self) -> str:
        return datetime.fromtimestamp(self.now(), PACIFIC).date().isoformat()

    def used(self) -> int:
        return db.upload_bytes(self.conn, self.day())

    def add(self, n: int) -> None:
        db.add_upload_bytes(self.conn, self.day(), n)

    def block(self, seconds: float) -> None:
        db.kv_set(self.conn, "blocked_until", str(self.now() + seconds))

    def _blocked_until(self) -> float:
        return float(db.kv_get(self.conn, "blocked_until", "0"))

    def allows(self, n: int) -> bool:
        if self.now() < self._blocked_until():
            return False
        used = self.used()
        return used == 0 or used + n <= self.limit

    def seconds_until_reset(self) -> float:
        now = self.now()
        blocked = self._blocked_until()
        if blocked > now:
            return blocked - now
        today = datetime.fromtimestamp(now, PACIFIC).date()
        midnight = datetime.combine(today + timedelta(days=1), dtime(0), PACIFIC)
        return max(midnight.timestamp() - now, 1.0)
