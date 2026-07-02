# IDX Alert Bot

A Telegram bot that watches [Bursa Efek Indonesia (IDX)](https://www.idx.co.id)
company announcements, filters them by keyword (and optionally by ticker), and
pushes an alert — with the announcement PDF attached — to your channel.

It polls **fast during market hours** (every few seconds) and slows down after
close, and it only ever alerts announcements that are genuinely *newer* than the
last one it has seen — so it never re-sends old items that appear late in the
feed.

```
🚨 ALERT PENGUMUMAN BEI

Keyword: Penambahan Modal
Emiten: PEGE

Subject: Keterbukaan Informasi terkait Aksi Korporasi - Rencana
Penambahan Modal dengan HMETD - 30062026 [PEGE]
Time: 2026-06-30T11:06:00

Link:
https://www.idx.co.id/StaticData/NewsAndAnnouncement/.../cdec57b527_ee07c4f5a9.pdf
[cdec57b527_ee07c4f5a9.pdf]
```

## How it works

IDX sits behind **Cloudflare's bot challenge**, so a plain HTTP request to the
announcement API returns a `403 "Just a moment..."` page. To get past it the bot
drives a real headless Chromium via **Playwright**:

1. **Warmup** — load the announcements page so Cloudflare runs its JS challenge
   and grants a `cf_clearance` cookie. The page is kept open and reused.
2. **Fetch** — call `GetAnnouncement` from *inside* that page via `fetch()`, so
   the request uses Chromium's real network stack (correct TLS fingerprint +
   the clearance cookie). This is essential: Playwright's `context.request` is a
   separate HTTP client with a different fingerprint and still gets 403'd.
3. **Filter** — keep announcements whose subject matches any configured keyword
   (and, if set, whose ticker is in the emiten whitelist).
4. **Dedupe + high-water-mark** — skip anything already in `seen.json`, and skip
   anything whose publish time is not newer than the latest already processed.
   The first poll after a fresh start *primes* the store silently (no backlog
   spam) and records the high-water-mark.
5. **Deliver** — download the PDF through the same page and send it to Telegram
   as a document with the alert as caption (link-only fallback if the download
   fails or is too large).

If clearance expires mid-run (a fetch 403s), the bot resets and re-warms on the
next tick automatically.

### Adaptive polling (market hours)

Polling speed follows the exchange clock (see `schedule.py`):

- **Market open** (Mon–Fri, inside the `08:45`–`15:00` WIB window by default) →
  fast interval (`poll.market_interval_seconds`, default **5s**).
- **Outside those hours** → slow interval (`poll.off_interval_seconds`, default
  **300s**), which saves resources and reduces the chance of being rate-limited.

Time is computed from UTC + a fixed offset (`schedule.utc_offset_hours`, default
`7` for WIB — Indonesia has no daylight saving), so it behaves the same on
Windows and in Docker.

### "Only alert newer news" (high-water-mark)

Beyond plain dedupe, the bot stores the **latest publish time it has processed**
in `seen.json`. On each poll it only alerts items whose `TglPengumuman` is
strictly newer than that mark. So:

- If no new announcement appears, nothing is sent.
- If IDX surfaces an *older* announcement late (backfill), it is **not** sent —
  the bot waits for genuinely newer items instead.

The mark persists across restarts, so a restart won't replay old announcements.

## Module map

| File | Responsibility |
|------|----------------|
| `idxbot/config.py` | Load `config.yaml` + secrets from env/`.env`; validate. |
| `idxbot/models.py` | Normalize IDX's messy JSON into an `Announcement` (incl. `published_dt`). |
| `idxbot/scraper.py` | Playwright browser page; Cloudflare warmup, in-page JSON fetch, PDF download. |
| `idxbot/filters.py` | Keyword + emiten matching. |
| `idxbot/schedule.py` | Adaptive interval based on the market-hours window (WIB). |
| `idxbot/state.py` | Persistent dedupe store + high-water-mark (`seen.json`), atomic writes. |
| `idxbot/telegram.py` | Format + send alerts via the Telegram Bot API. |
| `idxbot/poller.py` | The fetch→filter→dedupe→deliver loop with adaptive timing. |
| `idxbot/__main__.py` | CLI entrypoint (`--check`, `--once`, `--verbose`). |

## Setup

### 1. Create a Telegram bot & channel

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Create your channel, add the bot as an **admin** (needs post permission).
3. Get the chat id: use `@your_channel_username`, or for a private channel the
   numeric `-100...` id (e.g. via [@userinfobot] or the `getUpdates` API).

### 2. Configure

```bash
cp .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
# edit config.yaml: set your keywords / emiten watchlist / schedule
```

## Run

### Docker (recommended for a VPS)

```bash
docker compose up -d --build
docker compose logs -f
```

State persists in `./data/seen.json`; edit `config.yaml` and
`docker compose restart` to change filters without a rebuild.

### Locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium          # downloads the browser (~one-time)

python -m idxbot --check             # verify Telegram wiring
python -m idxbot --once --verbose    # single poll cycle (primes state, no alerts)
python -m idxbot                     # run the loop
```

> First real run **primes** the dedupe store with current announcements, records
> the high-water-mark, and sends no alerts. Alerts start from the next
> genuinely-newer announcement onward.

## Configuration reference

See `config.yaml` for inline docs. Key options:

- `poll.market_interval_seconds` — poll interval during market hours (min 3;
  default 5).
- `poll.off_interval_seconds` — poll interval outside market hours (default 300;
  must be ≥ the market interval).
- `schedule.market_open` / `schedule.market_close` — `HH:MM` window in local
  (WIB) time. Open bound is inclusive, close bound exclusive.
- `schedule.weekdays` — active days, `0`=Mon … `6`=Sun (default Mon–Fri).
- `schedule.utc_offset_hours` — timezone offset for the window (default 7, WIB).
- `filter.keywords` — case-insensitive substring match on the subject. Empty = all.
- `filter.emiten` — ticker whitelist. Empty = all tickers. When set, an item must
  match a keyword **and** be from a listed ticker.
- `download.attach_pdf` / `download.max_pdf_bytes` — attach the PDF, with a size cap.

## Notes & caveats

- **Public data, not a head start.** This reacts quickly to *publicly published*
  announcements; once IDX publishes, everyone sees it at the same time. A 5s
  interval is near the safe floor — if you start seeing 403s during busy hours,
  raise it to 8–10s.
- **Cloudflare may change.** If IDX escalates its challenge, warmup may need a
  longer wait or a residential proxy. The scraper isolates all of this in
  `scraper.py`.
- IDX field names have changed across site versions; `models.py::from_idx_row`
  tries multiple spellings. If alerts show blank fields, check the raw JSON with
  `python -m idxbot --once --verbose` and add the new key names there.

## Tests

```bash
pip install pytest
pytest
```

Offline tests cover filtering, dedupe/state persistence, the high-water-mark,
the market-hours scheduler, model normalization, and message formatting (no
network / browser required).
