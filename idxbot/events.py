"""Broadcaster Server-Sent Events (SSE) untuk push realtime ke dashboard.

Poll loop (thread daemon) memanggil `publish()` saat ada alert baru / update
status. Tiap koneksi browser mendaftar lewat `subscribe()` dan menerima antrian
sendiri, jadi satu klien lambat tidak memblok yang lain.

Sengaja tanpa dependensi: memakai queue.Queue (thread-safe) dan generator biasa
yang dibaca FastAPI StreamingResponse. Bounded queue supaya klien yang macet
tidak bikin memori membengkak — event tertua dibuang bila penuh.
"""
from __future__ import annotations

import json
import logging
import queue
import threading

log = logging.getLogger("idxbot.events")

_MAX_QUEUED = 100  # cap per-subscriber; drop-oldest bila klien tertinggal


class EventBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_MAX_QUEUED)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, data: dict) -> None:
        """Kirim event ke semua subscriber. Aman dipanggil dari thread poller."""
        payload = {"event": event, "data": data}
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Klien tertinggal: buang event tertua, sisipkan yang baru.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def format_sse(event: str, data: dict) -> str:
    """Bentuk satu frame SSE sesuai spesifikasi (event: + data:)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
