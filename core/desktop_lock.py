"""One physical desktop lease; background conversation and audio remain independent."""
from contextlib import contextmanager
import threading

LOCK = threading.RLock()
# A timed-out caller must not let another task race a provider call still in flight.
IN_FLIGHT = threading.Event()


@contextmanager
def acquire(blocking=False, timeout=None):
    if timeout is None:
        acquired = LOCK.acquire(blocking=blocking)
    else:
        acquired = LOCK.acquire(blocking=blocking, timeout=timeout)
    if acquired and IN_FLIGHT.is_set():
        LOCK.release()
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            LOCK.release()
