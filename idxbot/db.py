"""Riwayat pengumuman untuk dashboard — SQLite, ADITIF.

Ini BUKAN pengganti SeenStore/seen.json. Dedupe + high-water-mark tetap di
`state.py` (teruji, tidak disentuh). Store ini hanya menyimpan riwayat
pengumuman yang lolos filter agar dashboard bisa menampilkan feed + agregasi
untuk chart. Aman bila gagal: kegagalan pencatatan tidak boleh menjatuhkan
poll loop (pengiriman Telegram lebih penting daripada baris riwayat).

Thread-safety: poll loop (satu thread) menulis; request FastAPI membaca dari
thread lain. SQLite mengizinkan koneksi dipakai lintas-thread bila
`check_same_thread=False`, dan kita serialize semua akses dengan satu Lock.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Announcement

log = logging.getLogger("idxbot.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key         TEXT PRIMARY KEY,   -- dedupe key pengumuman (Announcement.key)
    emiten      TEXT NOT NULL,
    title       TEXT NOT NULL,
    keyword     TEXT,               -- keyword yang memicu match
    published   TEXT,               -- string mentah dari IDX (TglPengumuman)
    published_dt TEXT,              -- ISO ternormalisasi, atau NULL bila tak terparse
    link        TEXT,
    alerted_at  TEXT NOT NULL       -- kapan bot mengirim (ISO UTC)
);
CREATE INDEX IF NOT EXISTS idx_alerts_alerted_at ON alerts(alerted_at);
CREATE INDEX IF NOT EXISTS idx_alerts_emiten ON alerts(emiten);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HistoryStore:
    def __init__(self, path: str = "data/history.db"):
        # Pastikan direktori induk ada (mis. volume ./data yang di-mount),
        # kalau tidak sqlite3.connect gagal "unable to open database file".
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: dipakai lintas-thread, dijaga oleh _lock.
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_alert(self, ann: Announcement, keyword: str) -> None:
        """Simpan satu pengumuman ter-alert. Idempoten (INSERT OR IGNORE).

        Tidak pernah melempar ke pemanggil: riwayat bersifat best-effort.
        """
        dt = ann.published_dt
        row = (
            ann.key,
            ann.emiten or "",
            ann.title or "",
            keyword or "",
            ann.published or "",
            dt.isoformat() if dt is not None else None,
            ann.primary_link or "",
            _utcnow_iso(),
        )
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO alerts "
                    "(key, emiten, title, keyword, published, published_dt, link, alerted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                self._conn.commit()
        except sqlite3.Error as exc:  # noqa: BLE001 — riwayat tak boleh menjatuhkan poll
            log.warning("Gagal mencatat riwayat alert %s: %s", ann.key, exc)

    def recent(self, limit: int = 50) -> list[dict]:
        """Alert terbaru dulu, untuk feed dashboard."""
        limit = max(1, min(limit, 500))
        with self._lock:
            cur = self._conn.execute(
                "SELECT emiten, title, keyword, published, link, alerted_at "
                "FROM alerts ORDER BY alerted_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def count_total(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM alerts")
            return int(cur.fetchone()["n"])

    def top_emiten(self, limit: int = 10) -> list[dict]:
        """Agregasi jumlah alert per emiten (untuk bar chart)."""
        limit = max(1, min(limit, 50))
        with self._lock:
            cur = self._conn.execute(
                "SELECT emiten, COUNT(*) AS n FROM alerts "
                "GROUP BY emiten ORDER BY n DESC, emiten ASC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def per_day(self, days: int = 14) -> list[dict]:
        """Jumlah alert per hari (UTC) untuk sparkline/line chart, urut lama->baru."""
        days = max(1, min(days, 90))
        with self._lock:
            cur = self._conn.execute(
                "SELECT substr(alerted_at, 1, 10) AS day, COUNT(*) AS n "
                "FROM alerts GROUP BY day ORDER BY day DESC LIMIT ?",
                (days,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()  # tampilkan kronologis lama -> baru
        return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()
