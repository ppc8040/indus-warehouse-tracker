"""
Idempotency — prevents duplicate writes from double-taps / network retries.
In-memory with TTL. Upgrade to Redis for multi-dyno.
"""
import time
import threading
import hashlib

_lock = threading.Lock()
_seen = {}
TTL = 300


def make_key(handling_unit, movement_type, timestamp):
    raw = f"{handling_unit}:{movement_type}:{timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def reserve(key):
    """Returns True if newly reserved (proceed). False if duplicate (block)."""
    now = time.time()
    with _lock:
        expired = [k for k, ts in _seen.items() if now - ts > TTL]
        for k in expired:
            del _seen[k]
        if key in _seen:
            return False
        _seen[key] = now
        return True


def release(key):
    with _lock:
        _seen.pop(key, None)


def pending_count():
    now = time.time()
    with _lock:
        return sum(1 for ts in _seen.values() if now - ts <= TTL)
