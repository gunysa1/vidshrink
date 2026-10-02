from datetime import datetime

from vidshrink.budget import PACIFIC, Budget


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def noon():
    return datetime(2026, 9, 27, 12, 0, tzinfo=PACIFIC).timestamp()


def test_day_is_pacific(conn):
    # 06:00 UTC on the 28th is still the 27th in Pacific time.
    t = datetime.fromisoformat("2026-09-28T06:00:00+00:00").timestamp()
    assert Budget(conn, 100, Clock(t)).day() == "2026-09-27"


def test_allows_until_limit(conn):
    b = Budget(conn, 100, Clock(noon()))
    assert b.allows(100)
    b.add(60)
    assert b.allows(40) and not b.allows(41)


def test_oversized_file_allowed_on_fresh_day(conn):
    assert Budget(conn, 100, Clock(noon())).allows(500)


def test_resets_at_pacific_midnight(conn):
    clock = Clock(noon())
    b = Budget(conn, 100, clock)
    b.add(100)
    assert not b.allows(1)
    assert b.seconds_until_reset() == 12 * 3600
    clock.t += 12 * 3600
    assert b.allows(1)


def test_block_overrides_budget(conn):
    clock = Clock(noon())
    b = Budget(conn, 100, clock)
    b.block(6 * 3600)
    assert not b.allows(1)
    assert b.seconds_until_reset() == 6 * 3600
    clock.t += 6 * 3600
    assert b.allows(1)


def test_download_budget_is_separate_from_uploads(conn):
    clock = Clock(noon())
    up = Budget(conn, 100, clock)
    down = Budget(conn, 50, clock, kind="download")
    up.add(100)
    assert down.used() == 0 and down.allows(50)
    down.add(50)
    assert up.used() == 100 and not down.allows(1)
    down.block(3600)
    assert down.seconds_until_reset() == 3600
    assert up.seconds_until_reset() == 12 * 3600  # upload block untouched
