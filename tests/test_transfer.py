import threading

import pytest

from vidshrink.transfer import Transfer


def test_transfer_reports_success_and_exception():
    gate = threading.Event()
    ran = []
    ok = Transfer(lambda: (gate.wait(5), ran.append(1)))
    assert not ok.done()
    with pytest.raises(RuntimeError):
        ok.result()                     # not finished yet
    gate.set()
    assert ok.wait(5) and ok.done()
    assert ok.result() is None and ran == [1]

    def boom():
        raise ValueError("transfer failed")

    bad = Transfer(boom)
    assert bad.wait(5)
    with pytest.raises(ValueError, match="transfer failed"):
        bad.result()


def test_transfer_cancel_sets_its_event_and_waits_briefly():
    cancel = threading.Event()
    stopped = []

    def fn():
        cancel.wait(5)
        stopped.append(cancel.is_set())

    t = Transfer(fn, cancel=cancel)
    t.cancel(timeout=5)
    assert t.done() and stopped == [True]
