"""Configuration + environment loading.

Runtime config lives in config.yaml (behaviour, filters); secrets live in
environment variables / .env (Telegram token, chat id). This split keeps the
committed config safe to share.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@dataclass
class PollConfig:
    # Interval saat jam bursa buka (detik) — polling cepat untuk reaksi cepat.
    market_interval_seconds: int = 5
    # Interval di luar jam bursa (detik) — hemat sumber daya & hindari blokir.
    off_interval_seconds: int = 300
    page_size: int = 20
    lang: str = "id"


@dataclass
class ScheduleConfig:
    # Bursa Indonesia = WIB (UTC+7), tidak ada DST, jadi offset tetap.
    utc_offset_hours: int = 7
    # Jendela jam bursa (format "HH:MM", waktu lokal sesuai utc_offset_hours).
    market_open: str = "08:45"
    market_close: str = "15:00"
    # Hari aktif bursa: 0=Senin ... 6=Minggu. Default Senin-Jumat.
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])


@dataclass
class FilterConfig:
    keywords: list[str] = field(default_factory=list)
    emiten: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    channel_title: str = "Alert IDX Channel"


@dataclass
class DownloadConfig:
    attach_pdf: bool = True
    max_pdf_bytes: int = 20 * 1024 * 1024


@dataclass
class Config:
    poll: PollConfig
    schedule: ScheduleConfig
    filter: FilterConfig
    telegram: TelegramConfig
    download: DownloadConfig

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Load .env then config.yaml, layering secrets from the environment."""
        load_dotenv()

        cfg_path = Path(path or os.getenv("IDX_CONFIG", "config.yaml"))
        if not cfg_path.exists():
            raise ConfigError(f"Config file not found: {cfg_path}")

        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config.yaml must be a mapping of sections.")

        def _section(name: str, cls):
            data = raw.get(name) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"Section '{name}' must be a mapping.")
            try:
                return cls(**data)
            except TypeError as exc:
                # Typo'd key -> readable error instead of a raw TypeError.
                raise ConfigError(f"Unknown option in '{name}': {exc}") from exc

        poll = _section("poll", PollConfig)
        schedule = _section("schedule", ScheduleConfig)
        flt = _section("filter", FilterConfig)
        dl = _section("download", DownloadConfig)

        tg_raw = raw.get("telegram") or {}
        telegram = TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            channel_title=tg_raw.get("channel_title", "Alert IDX Channel"),
        )

        cfg = cls(
            poll=poll, schedule=schedule, filter=flt, telegram=telegram, download=dl
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.telegram.bot_token or self.telegram.bot_token.startswith("123456:ABC"):
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
        if ":" not in self.telegram.bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN looks malformed (expected '<id>:<secret>').")
        if not self.telegram.chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID is not set (see .env.example).")
        if self.poll.market_interval_seconds < 3:
            raise ConfigError("poll.market_interval_seconds must be >= 3 to avoid rate-limiting.")
        if self.poll.off_interval_seconds < self.poll.market_interval_seconds:
            raise ConfigError("poll.off_interval_seconds must be >= market_interval_seconds.")
        if not (1 <= self.poll.page_size <= 200):
            raise ConfigError("poll.page_size must be between 1 and 200.")
        if self.poll.lang not in ("id", "en"):
            raise ConfigError("poll.lang must be 'id' or 'en'.")
        if self.download.max_pdf_bytes <= 0:
            raise ConfigError("download.max_pdf_bytes must be positive.")
        if self.download.max_pdf_bytes > 50 * 1024 * 1024:
            raise ConfigError("download.max_pdf_bytes exceeds Telegram's 50MB bot upload limit.")
        if not self.schedule.weekdays:
            raise ConfigError("schedule.weekdays must not be empty.")
        if any(not isinstance(d, int) or not 0 <= d <= 6 for d in self.schedule.weekdays):
            raise ConfigError("schedule.weekdays entries must be integers 0 (Mon) .. 6 (Sun).")
        if not -12 <= self.schedule.utc_offset_hours <= 14:
            raise ConfigError("schedule.utc_offset_hours must be between -12 and 14.")
        # Validate the HH:MM window strings up front so a typo fails fast.
        parsed: dict[str, int] = {}
        for label, value in (
            ("schedule.market_open", self.schedule.market_open),
            ("schedule.market_close", self.schedule.market_close),
        ):
            try:
                hh, mm = value.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
                parsed[label] = int(hh) * 60 + int(mm)
            except (ValueError, AttributeError):
                raise ConfigError(f"{label} must be 'HH:MM' (24h), got {value!r}.")
        if parsed["schedule.market_open"] >= parsed["schedule.market_close"]:
            raise ConfigError("schedule.market_open must be earlier than market_close.")
