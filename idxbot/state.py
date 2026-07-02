"""Persistent dedupe store + high-water-mark.

Dua fungsi:
  * dedupe: mengingat key pengumuman yang sudah di-alert agar restart / poll yang
    tumpang tindih tidak mengirim ganda.
  * high-water-mark: menyimpan waktu publish terbaru yang sudah diproses, sehingga
    bot hanya alert item yang BENAR-BENAR lebih baru. Ini mencegah pengiriman
    pengumuman "berjam di belakang" yang telat muncul (backfill) di feed IDX.

Backed by a small JSON file; the seen-set is capped so it can't grow unbounded.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path


class SeenStore:
    def __init__(self, path: str | os.PathLike = "seen.json", max_entries: int = 5000):
        self._path = Path(path)
        self._max = max_entries
        self._order: deque[str] = deque(maxlen=max_entries)
        self._set: set[str] = set()
        self._high_water: str = ""  # ISO string; "" berarti belum ada
        # Ensure the parent dir exists (e.g. the mounted ./data volume).
        if self._path.parent and not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            keys = data.get("seen", []) if isinstance(data, dict) else data
            if not isinstance(keys, list):
                keys = []
            for k in keys[-self._max :]:
                if isinstance(k, str) and k:  # skip junk entries from bad writes
                    self._order.append(k)
                    self._set.add(k)
            if isinstance(data, dict):
                hw = data.get("high_water", "")
                self._high_water = hw if isinstance(hw, str) else ""
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable state should not crash the bot; start fresh —
            # but keep the broken file aside for post-mortem instead of losing it.
            self._order.clear()
            self._set.clear()
            self._high_water = ""
            try:
                self._path.replace(self._path.with_suffix(".corrupt"))
            except OSError:
                pass

    def has(self, key: str) -> bool:
        return key in self._set

    def add(self, key: str) -> None:
        if key in self._set:
            return
        if len(self._order) == self._max and self._order:
            evicted = self._order[0]
            self._set.discard(evicted)
        self._order.append(key)
        self._set.add(key)

    # -- high-water-mark ---------------------------------------------------
    @property
    def high_water(self) -> datetime | None:
        """Waktu publish terbaru yang sudah diproses, atau None."""
        if not self._high_water:
            return None
        try:
            return datetime.fromisoformat(self._high_water)
        except ValueError:
            return None

    def bump_high_water(self, dt: datetime | None) -> None:
        """Naikkan high-water-mark bila `dt` lebih baru dari yang tersimpan."""
        if dt is None:
            return
        current = self.high_water
        if current is None or dt > current:
            self._high_water = dt.isoformat()

    def save(self) -> None:
        """Atomic write so a crash mid-save can't corrupt the file."""
        payload = {"seen": list(self._order), "high_water": self._high_water}
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent or "."), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, self._path)
        except OSError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def __len__(self) -> int:
        return len(self._set)
