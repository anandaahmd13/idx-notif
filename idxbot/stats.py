"""Metrik live bot untuk halaman status/monitoring.

Objek `BotStats` dibagikan antara poll loop (penulis) dan web server (pembaca)
yang berjalan di thread berbeda dalam satu proses, jadi semua akses dijaga oleh
satu Lock. `snapshot()` mengembalikan dict yang aman di-serialize ke JSON.

Bila web dimatikan, Poller tetap jalan tanpa objek ini (stats=None) sehingga
bot Telegram murni tidak menanggung overhead apa pun.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = _utcnow()
        self._last_poll_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._poll_count = 0
        self._consecutive_failures = 0
        self._total_alerts = 0
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._market_open: bool | None = None
        self._current_interval: int | None = None
        self._retry_queue_size = 0

    # -- writers (dipanggil dari poll loop) --------------------------------
    def mark_poll_start(self) -> None:
        with self._lock:
            self._poll_count += 1
            self._last_poll_at = _utcnow()

    def mark_poll_success(self) -> None:
        with self._lock:
            self._last_success_at = _utcnow()
            self._consecutive_failures = 0

    def mark_poll_failure(self, error: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = error
            self._last_error_at = _utcnow()

    def add_alerts(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._total_alerts += n

    def set_schedule(self, market_open: bool, interval: int) -> None:
        with self._lock:
            self._market_open = market_open
            self._current_interval = interval

    def set_retry_queue_size(self, size: int) -> None:
        with self._lock:
            self._retry_queue_size = size

    # -- reader (dipanggil dari web server) --------------------------------
    def is_healthy(self, now: datetime | None = None) -> bool:
        """Sehat bila poll terakhir sukses dalam ~3x interval terakhir.

        Sebelum poll sukses pertama kali (mis. tepat setelah start) dianggap
        sehat selama belum ada kegagalan beruntun yang menumpuk.
        """
        now = now or _utcnow()
        with self._lock:
            return self._is_healthy_locked(now)

    def _is_healthy_locked(self, now: datetime) -> bool:
        """Inti perhitungan sehat; pemanggil HARUS sudah memegang _lock."""
        if self._consecutive_failures >= 3:
            return False
        if self._last_success_at is None:
            # Belum sempat sukses; beri kelonggaran saat boot.
            return self._consecutive_failures < 3
        interval = self._current_interval or 60
        age = (now - self._last_success_at).total_seconds()
        return age <= max(interval * 3, 30)

    def snapshot(self, now: datetime | None = None) -> dict:
        """Dict JSON-serializable untuk /api/status."""
        now = now or _utcnow()
        with self._lock:
            uptime = (now - self._started_at).total_seconds()
            return {
                "healthy": self._is_healthy_locked(now),
                "started_at": self._started_at.isoformat(),
                "uptime_seconds": round(uptime),
                "last_poll_at": _iso(self._last_poll_at),
                "last_success_at": _iso(self._last_success_at),
                "poll_count": self._poll_count,
                "consecutive_failures": self._consecutive_failures,
                "total_alerts": self._total_alerts,
                "last_error": self._last_error,
                "last_error_at": _iso(self._last_error_at),
                "market_open": self._market_open,
                "current_interval_seconds": self._current_interval,
                "retry_queue_size": self._retry_queue_size,
            }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None
