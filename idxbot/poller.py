"""The polling loop: fetch -> filter -> dedupe -> download -> notify.

Dua perilaku penting:

* Prime saat start: poll pertama menandai semua pengumuman yang sedang tampil
  sebagai "sudah dilihat" TANPA alert, sekaligus menyetel high-water-mark ke
  waktu publish terbaru. Jadi bot tidak membanjiri channel dengan backlog lama.
* High-water-mark: setelah prime, bot HANYA alert item yang waktu publish-nya
  lebih baru dari yang terakhir diproses. Ini mencegah pengiriman pengumuman
  "berjam di belakang" yang telat tampil (backfill) di feed IDX.

Interval polling adaptif: cepat saat jam bursa buka, lambat di luar jam itu
(lihat schedule.py).
"""
from __future__ import annotations

import logging
import os
import tempfile
import time

from .config import Config
from .filters import AnnouncementFilter
from .models import Announcement
from .schedule import MarketSchedule
from .scraper import IdxScraper, ScrapeError
from .state import SeenStore
from .telegram import TelegramNotifier

log = logging.getLogger("idxbot.poller")

# Restart Chromium setelah sekian kegagalan poll beruntun — biasanya berarti
# browser/renderer sudah rusak, bukan sekadar Cloudflare sesaat.
MAX_CONSECUTIVE_FAILURES = 5
# Alert yang gagal terkirim dicoba ulang maksimal sekian siklus poll.
MAX_DELIVERY_ATTEMPTS = 3


class Poller:
    def __init__(self, cfg: Config, seen_path: str = "seen.json"):
        self._cfg = cfg
        self._filter = AnnouncementFilter(cfg.filter)
        self._seen = SeenStore(seen_path)
        self._notifier = TelegramNotifier(cfg.telegram)
        self._schedule = MarketSchedule(cfg.schedule, cfg.poll)
        self._prime_done = len(self._seen) > 0  # already primed in a past run
        # State lama (versi sebelum high-water) punya seen tapi tanpa mark;
        # set mark pada poll live pertama tanpa alert backlog.
        self._hw_needs_init = self._prime_done and self._seen.high_water is None
        # Antrian retry: alert yang sudah lolos filter tapi gagal dikirim
        # (Telegram down, dsb). Kunci dedupe-nya sudah tercatat di seen, jadi
        # tanpa antrian ini alert tersebut hilang selamanya.
        self._retry_queue: list[tuple[Announcement, object, int]] = []

    def run_forever(self) -> None:
        """Outer loop: rebuild the whole browser when it goes bad; inner loop
        polls until MAX_CONSECUTIVE_FAILURES back-to-back errors."""
        while True:
            try:
                self._run_with_scraper()
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 — browser/session level failure
                log.exception("Scraper session died; restarting browser in 30s.")
                time.sleep(30)

    def _run_with_scraper(self) -> None:
        with IdxScraper(
            page_size=self._cfg.poll.page_size, lang=self._cfg.poll.lang
        ) as scraper:
            log.info(
                "Scraper ready; interval bursa=%ss, di luar jam=%ss.",
                self._cfg.poll.market_interval_seconds,
                self._cfg.poll.off_interval_seconds,
            )
            last_open: bool | None = None
            failures = 0
            while True:
                started = time.monotonic()
                try:
                    self._poll_once(scraper)
                    failures = 0
                except ScrapeError as exc:
                    failures += 1
                    log.warning("Poll failed (%d berturut-turut): %s", failures, exc)
                except Exception:  # noqa: BLE001 — loop must survive unexpected errors
                    failures += 1
                    log.exception("Unexpected error during poll.")

                if failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "%d kegagalan beruntun — restart browser untuk pulih.", failures
                    )
                    return  # exit the `with` -> browser torn down -> outer loop rebuilds

                is_open = self._schedule.is_market_open()
                if is_open != last_open:
                    log.info(
                        "Jam bursa %s — interval %ss.",
                        "BUKA" if is_open else "tutup",
                        self._schedule.current_interval(),
                    )
                    last_open = is_open

                interval = self._schedule.current_interval()
                if failures:
                    # Backoff eksponensial saat error beruntun agar tidak
                    # menghajar Cloudflare — 2x, 4x, 8x... maks 10 menit.
                    interval = min(interval * (2 ** failures), 600)
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, interval - elapsed))

    def _poll_once(self, scraper: IdxScraper) -> None:
        # Kirim ulang alert yang gagal pada siklus sebelumnya SEBELUM fetch,
        # supaya urutan kronologis di channel tetap terjaga.
        self._flush_retry_queue(scraper)

        announcements = scraper.fetch_announcements()
        log.debug("Fetched %d announcements.", len(announcements))
        if not announcements:
            # Feed kosong hampir pasti berarti respons rusak/berubah bentuk,
            # bukan benar-benar nol pengumuman — jangan proses apa pun.
            raise ScrapeError("IDX returned zero parsable announcements.")

        if not self._prime_done:
            for ann in announcements:
                self._seen.add(ann.key)
                self._seen.bump_high_water(ann.published_dt)
            self._seen.save()
            self._prime_done = True
            self._hw_needs_init = False
            log.info(
                "Primed %d existing announcements (no alerts sent); high_water=%s.",
                len(self._seen),
                self._seen.high_water,
            )
            return

        if self._hw_needs_init:
            for ann in announcements:
                self._seen.bump_high_water(ann.published_dt)
            self._seen.save()
            self._hw_needs_init = False
            log.info("Inisialisasi high-water mark ke %s (no alerts).", self._seen.high_water)
            return

        # Mark saat poll dimulai; dipakai sebagai ambang untuk seluruh batch ini.
        hw = self._seen.high_water

        # IDX returns newest first; process oldest-first so channel order is chronological.
        new_alerts = 0
        for ann in reversed(announcements):
            if self._seen.has(ann.key):
                continue
            self._seen.add(ann.key)  # tandai terlihat agar tidak dievaluasi ulang

            dt = ann.published_dt
            # High-water filter: lewati item yang TIDAK lebih baru dari ambang.
            # (dt None = tak bisa diparse -> tetap diproses, default aman.)
            if hw is not None and dt is not None and dt <= hw:
                log.debug("Skip backfill lama: [%s] %s (%s)", ann.emiten, ann.title[:60], dt)
                continue

            match = self._filter.check(ann)
            # Naikkan mark untuk setiap item baru & tidak-lebih-lama, terlepas
            # dari cocok/tidaknya keyword, agar backfill setelahnya ikut tersaring.
            self._seen.bump_high_water(dt)
            if not match.matched:
                continue
            self._deliver(scraper, ann, match)
            new_alerts += 1

        self._seen.save()
        if new_alerts:
            log.info("Sent %d new alert(s).", new_alerts)

    def _deliver(
        self, scraper: IdxScraper, ann: Announcement, match, attempt: int = 1
    ) -> bool:
        """Download the PDF (best-effort) and send the alert.

        Returns True when delivered. On failure the alert is queued and
        retried on the next poll cycles (up to MAX_DELIVERY_ATTEMPTS total),
        because its dedupe key is already in `seen` and would otherwise be
        silently lost."""
        pdf_path: str | None = None
        tmp_name: str | None = None
        try:
            if self._cfg.download.attach_pdf and ann.primary_link:
                fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                try:
                    ok = scraper.download_pdf(
                        ann.primary_link, tmp_name, self._cfg.download.max_pdf_bytes
                    )
                    pdf_path = tmp_name if ok else None
                except ScrapeError as exc:
                    log.warning("PDF download failed (%s); sending link-only.", exc)

            sent = self._notifier.send(ann, match, pdf_path)
            if sent:
                log.info("Alerted: [%s] %s", ann.emiten, ann.title[:80])
                return True

            if attempt < MAX_DELIVERY_ATTEMPTS:
                self._retry_queue.append((ann, match, attempt + 1))
                log.error(
                    "Gagal kirim alert %s (percobaan %d) — antre untuk retry.",
                    ann.key, attempt,
                )
            else:
                log.error(
                    "Alert %s GAGAL PERMANEN setelah %d percobaan: [%s] %s",
                    ann.key, attempt, ann.emiten, ann.title[:80],
                )
            return False
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _flush_retry_queue(self, scraper: IdxScraper) -> None:
        if not self._retry_queue:
            return
        pending, self._retry_queue = self._retry_queue, []
        log.info("Mencoba ulang %d alert yang tertunda.", len(pending))
        for ann, match, attempt in pending:
            self._deliver(scraper, ann, match, attempt=attempt)
