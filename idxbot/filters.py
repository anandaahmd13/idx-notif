"""Keyword + emiten filtering.

An announcement passes when:
  * its title matches ANY configured keyword (case-insensitive substring), and
  * if an emiten whitelist is set, its ticker is in that whitelist.
Empty keyword list => match all subjects. Empty emiten list => all tickers.

Matching dinormalisasi: judul dan keyword sama-sama di-lowercase dan seluruh
run whitespace (spasi ganda, tab, NBSP dari copy-paste) diringkas jadi satu
spasi, sehingga "Penambahan  Modal" tetap cocok dengan "Penambahan Modal".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import FilterConfig
from .models import Announcement

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Lowercase + collapse all whitespace runs to single spaces."""
    return _WS.sub(" ", text).strip().lower()


@dataclass
class MatchResult:
    matched: bool
    keyword: str = ""  # the keyword that triggered the match (for the alert header)


class AnnouncementFilter:
    def __init__(self, cfg: FilterConfig):
        self._keywords = [k.strip() for k in cfg.keywords if k and k.strip()]
        self._keywords_norm = [_norm(k) for k in self._keywords]
        self._emiten = {e.strip().upper() for e in cfg.emiten if e and e.strip()}

    def check(self, ann: Announcement) -> MatchResult:
        if self._emiten and (ann.emiten or "").strip().upper() not in self._emiten:
            return MatchResult(matched=False)

        if not self._keywords_norm:
            return MatchResult(matched=True, keyword="")

        title_norm = _norm(ann.title or "")
        for original, normed in zip(self._keywords, self._keywords_norm):
            if normed in title_norm:
                return MatchResult(matched=True, keyword=original)

        return MatchResult(matched=False)
