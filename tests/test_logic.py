"""Offline tests: no network, no browser. Covers the pure logic."""
from __future__ import annotations

import json

import pytest

from idxbot.config import (
    DownloadConfig,
    FilterConfig,
    TelegramConfig,
)
from idxbot.filters import AnnouncementFilter
from idxbot.models import Announcement
from idxbot.state import SeenStore
from idxbot.telegram import TelegramNotifier
from idxbot.filters import MatchResult


# -- models -----------------------------------------------------------------
def test_from_idx_row_flat_fallback():
    """Flat row (no `pengumuman` wrapper) — the resilience fallback path."""
    row = {
        "Id2": "12345",
        "Kode_Emiten": "PEGE",
        "JudulPengumuman": "Rencana Penambahan Modal dengan HMETD [PEGE]",
        "TglPengumuman": "2026-06-30 11:06",
        "attachments": [
            {
                "FullSavePath": "/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202606/abc.pdf",
                "OriginalFilename": "abc.pdf",
                "FileSize": "7000",
            }
        ],
    }
    ann = Announcement.from_idx_row(row)
    assert ann.emiten == "PEGE"
    assert ann.id == "12345"
    assert "Penambahan Modal" in ann.title
    assert ann.primary_link.startswith("https://www.idx.co.id/StaticData/")
    assert ann.attachments[0].size == 7000


def test_from_idx_row_camelcase_and_backslashes():
    row = {
        "Kode_Emiten": "BBRI",
        "JudulPengumuman": "Dividen Tunai",
        "attachments": [{"path": "\\StaticData\\x\\y.pdf", "filename": "y.pdf"}],
    }
    ann = Announcement.from_idx_row(row)
    assert ann.emiten == "BBRI"
    assert ann.primary_link == "https://www.idx.co.id/StaticData/x/y.pdf"


def test_key_fallback_hash_when_no_id():
    a = Announcement(id="", emiten="AAA", title="t", published="p")
    b = Announcement(id="", emiten="AAA", title="t", published="p")
    c = Announcement(id="", emiten="AAA", title="other", published="p")
    assert a.key == b.key
    assert a.key != c.key


def test_from_idx_row_real_nested_shape():
    """The live IDX shape: fields under `pengumuman`, attachments as sibling."""
    row = {
        "pengumuman": {
            "Id": 0,  # always 0 in the feed — must NOT be used as the key
            "Id2": "20260701221959-137/CORSEC/PTP/VII2026_id-id",
            "NoPengumuman": "137/CORSEC/PTP/VII2026",
            "Kode_Emiten": "TRIN                          ",  # trailing whitespace
            "JudulPengumuman": "Laporan Pengalihan Kembali Saham Hasil Buy Back",
            "TglPengumuman": "2026-07-01T22:19:59",
        },
        "attachments": [
            {
                "PDFFilename": "main.pdf",
                "FullSavePath": "https://www.idx.co.id/StaticData/x/main.pdf",
                "IsAttachment": False,
            },
            {
                "PDFFilename": "lamp1.pdf",
                "FullSavePath": "https://www.idx.co.id/StaticData/x/lamp1.pdf",
                "IsAttachment": True,
            },
        ],
    }
    ann = Announcement.from_idx_row(row)
    assert ann.emiten == "TRIN"  # whitespace stripped
    assert ann.id == "20260701221959-137/CORSEC/PTP/VII2026_id-id"  # Id2, not Id=0
    assert ann.title.startswith("Laporan Pengalihan")
    # Main document (IsAttachment=false) must come first.
    assert ann.primary_link.endswith("/main.pdf")
    assert len(ann.attachments) == 2


def test_distinct_ids_across_rows():
    """Regression: rows must not collapse to one dedupe key (the 'Primed 1' bug)."""
    rows = [
        {"pengumuman": {"Id": 0, "Id2": "aaa", "Kode_Emiten": "AAA", "JudulPengumuman": "x"}},
        {"pengumuman": {"Id": 0, "Id2": "bbb", "Kode_Emiten": "BBB", "JudulPengumuman": "y"}},
    ]
    keys = {Announcement.from_idx_row(r).key for r in rows}
    assert len(keys) == 2


# -- filters ----------------------------------------------------------------
def _ann(title="", emiten="PEGE"):
    return Announcement(id="x" + title + emiten, emiten=emiten, title=title, published="-")


def test_keyword_match_case_insensitive():
    f = AnnouncementFilter(FilterConfig(keywords=["Penambahan Modal"], emiten=[]))
    res = f.check(_ann("rencana PENAMBAHAN modal dgn HMETD"))
    assert res.matched and res.keyword == "Penambahan Modal"


def test_no_keyword_match():
    f = AnnouncementFilter(FilterConfig(keywords=["Dividen"], emiten=[]))
    assert not f.check(_ann("Penambahan Modal")).matched


def test_empty_keywords_matches_all():
    f = AnnouncementFilter(FilterConfig(keywords=[], emiten=[]))
    assert f.check(_ann("anything at all")).matched


def test_emiten_whitelist_blocks_others():
    f = AnnouncementFilter(FilterConfig(keywords=["Modal"], emiten=["BBRI"]))
    assert not f.check(_ann("Penambahan Modal", emiten="PEGE")).matched
    assert f.check(_ann("Penambahan Modal", emiten="bbri")).matched  # case-insensitive


def test_emiten_set_but_keyword_still_required():
    f = AnnouncementFilter(FilterConfig(keywords=["Dividen"], emiten=["PEGE"]))
    # right emiten, wrong keyword -> no match
    assert not f.check(_ann("Penambahan Modal", emiten="PEGE")).matched


def test_keyword_matches_despite_extra_whitespace():
    """Judul IDX kadang punya spasi ganda/tab; normalisasi harus tetap match."""
    f = AnnouncementFilter(FilterConfig(keywords=["Penambahan Modal"], emiten=[]))
    assert f.check(_ann("Rencana  Penambahan\tModal dgn HMETD")).matched
    # Keyword dengan spasi ganda dari config juga dinormalisasi.
    f2 = AnnouncementFilter(FilterConfig(keywords=["Penambahan  Modal"], emiten=[]))
    assert f2.check(_ann("Rencana Penambahan Modal")).matched


def test_emiten_with_whitespace_still_matches_whitelist():
    """Kode_Emiten dari feed membawa trailing whitespace berat."""
    f = AnnouncementFilter(FilterConfig(keywords=[], emiten=["TRIN"]))
    assert f.check(_ann("apapun", emiten="TRIN   ")).matched


# -- state ------------------------------------------------------------------
def test_seen_store_persist_and_reload(tmp_path):
    p = tmp_path / "seen.json"
    s = SeenStore(p)
    s.add("a")
    s.add("b")
    s.save()

    s2 = SeenStore(p)
    assert s2.has("a") and s2.has("b")
    assert not s2.has("c")


def test_seen_store_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "seen.json"
    s = SeenStore(p)
    s.add("k")
    s.save()
    assert p.exists()


def test_seen_store_dedupes(tmp_path):
    s = SeenStore(tmp_path / "seen.json")
    s.add("a")
    s.add("a")
    assert len(s) == 1


def test_seen_store_eviction_cap(tmp_path):
    s = SeenStore(tmp_path / "seen.json", max_entries=3)
    for k in ["a", "b", "c", "d"]:
        s.add(k)
    assert len(s) == 3
    assert not s.has("a")  # oldest evicted
    assert s.has("d")


def test_seen_store_survives_corrupt_file(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = SeenStore(p)  # should not raise
    assert len(s) == 0
    # File rusak dipindahkan ke .corrupt untuk post-mortem, bukan dibuang.
    assert (tmp_path / "seen.corrupt").exists()


def test_seen_store_skips_junk_entries(tmp_path):
    """Entri non-string dalam file state di-skip, bukan bikin crash."""
    p = tmp_path / "seen.json"
    p.write_text(json.dumps({"seen": ["ok", 123, None, ""], "high_water": 42}), encoding="utf-8")
    s = SeenStore(p)
    assert s.has("ok")
    assert len(s) == 1
    assert s.high_water is None  # high_water non-string diabaikan


# -- telegram formatting ----------------------------------------------------
def test_build_text_layout():
    tg = TelegramNotifier(TelegramConfig(bot_token="t", chat_id="c"))
    ann = Announcement(
        id="1",
        emiten="PEGE",
        title="Rencana Penambahan Modal dengan HMETD [PEGE]",
        published="2026-06-30 11:06",
        attachments=[],
    )
    ann.attachments.append(
        __import__("idxbot.models", fromlist=["Attachment"]).Attachment(
            filename="x.pdf", url="https://www.idx.co.id/x.pdf"
        )
    )
    text = tg.build_text(ann, MatchResult(matched=True, keyword="Penambahan Modal"))
    assert "ALERT PENGUMUMAN BEI" in text
    assert "Keyword: Penambahan Modal" in text
    assert "Emiten: PEGE" in text
    assert "Subject: Rencana Penambahan Modal" in text
    assert "https://www.idx.co.id/x.pdf" in text


def test_build_text_escapes_html():
    tg = TelegramNotifier(TelegramConfig(bot_token="t", chat_id="c"))
    ann = Announcement(id="1", emiten="X&Y", title="a<b>c", published="-")
    text = tg.build_text(ann, MatchResult(matched=True, keyword="k"))
    assert "&amp;" in text and "&lt;b&gt;" in text


# -- published_dt parsing ---------------------------------------------------
def test_published_dt_iso_format():
    ann = Announcement(id="1", emiten="X", title="t", published="2026-07-01T22:19:59")
    dt = ann.published_dt
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 7, 1, 22, 19, 59)


def test_published_dt_space_format():
    ann = Announcement(id="1", emiten="X", title="t", published="2026-06-30 11:06:00")
    assert ann.published_dt is not None


def test_published_dt_unparseable_returns_none():
    assert Announcement(id="1", emiten="X", title="t", published="-").published_dt is None
    assert Announcement(id="1", emiten="X", title="t", published="").published_dt is None


def test_published_dt_with_offset_is_naive():
    """Timestamp ber-offset harus jadi naive agar bisa dibandingkan dg high-water."""
    ann = Announcement(id="1", emiten="X", title="t", published="2026-07-01T22:19:59.123+07:00")
    dt = ann.published_dt
    assert dt is not None and dt.tzinfo is None
    assert (dt.hour, dt.minute, dt.second) == (22, 19, 59)


# -- high-water-mark --------------------------------------------------------
from datetime import datetime


def test_high_water_bump_and_persist(tmp_path):
    p = tmp_path / "seen.json"
    s = SeenStore(p)
    assert s.high_water is None
    s.bump_high_water(datetime(2026, 7, 1, 10, 0, 0))
    s.bump_high_water(datetime(2026, 7, 1, 9, 0, 0))  # lebih lama -> diabaikan
    assert s.high_water == datetime(2026, 7, 1, 10, 0, 0)
    s.save()

    s2 = SeenStore(p)
    assert s2.high_water == datetime(2026, 7, 1, 10, 0, 0)


def test_high_water_ignores_none(tmp_path):
    s = SeenStore(tmp_path / "seen.json")
    s.bump_high_water(datetime(2026, 7, 1, 10, 0, 0))
    s.bump_high_water(None)  # tidak boleh menurunkan / error
    assert s.high_water == datetime(2026, 7, 1, 10, 0, 0)


def test_high_water_only_advances(tmp_path):
    s = SeenStore(tmp_path / "seen.json")
    s.bump_high_water(datetime(2026, 7, 1, 12, 0, 0))
    s.bump_high_water(datetime(2026, 7, 1, 13, 30, 0))
    assert s.high_water == datetime(2026, 7, 1, 13, 30, 0)


def test_legacy_seen_file_has_no_high_water(tmp_path):
    """File lama (list saja / tanpa high_water) tetap terbaca, mark = None."""
    p = tmp_path / "seen.json"
    p.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    s = SeenStore(p)
    assert s.has("a") and s.high_water is None


# -- config validation ------------------------------------------------------
from idxbot.config import Config, ConfigError, PollConfig, ScheduleConfig


def _valid_config(**overrides):
    base = dict(
        poll=PollConfig(),
        schedule=ScheduleConfig(),
        filter=FilterConfig(),
        telegram=TelegramConfig(bot_token="99:token", chat_id="-100123"),
        download=DownloadConfig(),
    )
    base.update(overrides)
    return Config(**base)


def test_validate_accepts_sane_defaults():
    _valid_config().validate()  # must not raise


@pytest.mark.parametrize(
    "overrides",
    [
        {"telegram": TelegramConfig(bot_token="tanpatitikdua", chat_id="c")},
        {"poll": PollConfig(page_size=0)},
        {"poll": PollConfig(page_size=999)},
        {"download": DownloadConfig(max_pdf_bytes=0)},
        {"download": DownloadConfig(max_pdf_bytes=60 * 1024 * 1024)},
        {"schedule": ScheduleConfig(weekdays=[])},
        {"schedule": ScheduleConfig(weekdays=[7])},
        {"schedule": ScheduleConfig(market_open="15:00", market_close="08:45")},
        {"schedule": ScheduleConfig(utc_offset_hours=99)},
    ],
)
def test_validate_rejects_bad_values(overrides):
    with pytest.raises(ConfigError):
        _valid_config(**overrides).validate()


# -- market schedule --------------------------------------------------------
from idxbot.schedule import MarketSchedule


def _sched():
    return MarketSchedule(
        ScheduleConfig(
            utc_offset_hours=7,
            market_open="08:45",
            market_close="15:00",
            weekdays=[0, 1, 2, 3, 4],
        ),
        PollConfig(market_interval_seconds=5, off_interval_seconds=300),
    )


def test_market_open_during_window_weekday():
    s = _sched()
    # 2026-07-01 adalah hari Rabu (weekday=2).
    wib_noon = datetime(2026, 7, 1, 12, 0, 0)
    assert s.is_market_open(wib_noon)
    assert s.current_interval(wib_noon) == 5


def test_market_closed_before_open():
    s = _sched()
    assert not s.is_market_open(datetime(2026, 7, 1, 8, 30, 0))  # sebelum 08:45
    assert s.current_interval(datetime(2026, 7, 1, 8, 30, 0)) == 300


def test_market_closed_after_close():
    s = _sched()
    assert not s.is_market_open(datetime(2026, 7, 1, 15, 0, 1))  # setelah 15:00
    assert not s.is_market_open(datetime(2026, 7, 1, 23, 0, 0))


def test_market_closed_on_weekend():
    s = _sched()
    # 2026-07-04 = Sabtu, 2026-07-05 = Minggu.
    assert not s.is_market_open(datetime(2026, 7, 4, 12, 0, 0))
    assert not s.is_market_open(datetime(2026, 7, 5, 12, 0, 0))


def test_market_boundary_open_inclusive_close_exclusive():
    s = _sched()
    assert s.is_market_open(datetime(2026, 7, 1, 8, 45, 0))  # tepat buka -> masuk
    assert not s.is_market_open(datetime(2026, 7, 1, 15, 0, 0))  # tepat tutup -> keluar
