"""Interval adaptif berdasarkan jam bursa (WIB).

Saat jam bursa buka (Senin-Jumat, dalam jendela market_open..market_close)
poller memakai interval cepat; di luar itu memakai interval lambat untuk
menghemat sumber daya dan mengurangi risiko diblokir Cloudflare.

Waktu dihitung dari UTC + offset tetap (Indonesia tidak punya DST), jadi tidak
perlu database timezone dan hasilnya konsisten di Windows maupun Docker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import PollConfig, ScheduleConfig


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


class MarketSchedule:
    def __init__(self, schedule: ScheduleConfig, poll: PollConfig):
        self._tz = timezone(timedelta(hours=schedule.utc_offset_hours))
        self._open = _parse_hhmm(schedule.market_open)
        self._close = _parse_hhmm(schedule.market_close)
        self._weekdays = set(schedule.weekdays)
        self._market_interval = poll.market_interval_seconds
        self._off_interval = poll.off_interval_seconds

    def now_local(self) -> datetime:
        """Waktu lokal bursa (WIB) saat ini."""
        return datetime.now(timezone.utc).astimezone(self._tz)

    def is_market_open(self, at: datetime | None = None) -> bool:
        now = at or self.now_local()
        if now.weekday() not in self._weekdays:
            return False
        minutes = now.hour * 60 + now.minute
        open_min = self._open[0] * 60 + self._open[1]
        close_min = self._close[0] * 60 + self._close[1]
        return open_min <= minutes < close_min

    def current_interval(self, at: datetime | None = None) -> int:
        """Interval polling (detik) sesuai jam sekarang."""
        return self._market_interval if self.is_market_open(at) else self._off_interval
