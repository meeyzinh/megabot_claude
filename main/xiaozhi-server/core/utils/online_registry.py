"""Theo dõi thiết bị đang kết nối WebSocket (dùng cho trang admin hiển thị Online/Offline)."""
import time

_connections = {}  # device_id(lower) -> {"count": int, "since": float}


def _key(device_id):
    return str(device_id or "").strip().lower()


def register(device_id):
    key = _key(device_id)
    if not key:
        return
    entry = _connections.setdefault(key, {"count": 0, "since": time.time()})
    entry["count"] += 1


def unregister(device_id):
    key = _key(device_id)
    entry = _connections.get(key)
    if not entry:
        return
    entry["count"] -= 1
    if entry["count"] <= 0:
        _connections.pop(key, None)


def is_online(*identifiers):
    return any(_key(i) in _connections for i in identifiers if i)


def online_ids():
    return list(_connections.keys())
