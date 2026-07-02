"""The normalized Announcement record shared across scraper, filter, and notifier.

IDX's GetAnnouncement JSON is messy and its field names have shifted over time,
so we normalize into one stable dataclass here and keep all the field-name
guesswork in `from_idx_row`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

IDX_BASE = "https://www.idx.co.id"


def _first(row: dict, *keys: str, default: str = "") -> str:
    """Return the first present, non-empty value among candidate keys.

    IDX has used both PascalCase and camelCase across versions, so we try
    several spellings for each logical field.
    """
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return default


@dataclass
class Attachment:
    filename: str
    url: str
    size: int = 0


@dataclass
class Announcement:
    id: str
    emiten: str          # ticker code, e.g. "PEGE"
    title: str           # subject / judul pengumuman
    published: str       # raw datetime string from IDX ("Time:" in the alert)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable dedupe key. Prefer IDX's own id; fall back to a content hash."""
        if self.id:
            return self.id
        raw = f"{self.emiten}|{self.title}|{self.published}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @property
    def primary_link(self) -> str:
        return self.attachments[0].url if self.attachments else ""

    @property
    def published_dt(self) -> datetime | None:
        """Waktu publish sebagai datetime naive, atau None jika tak bisa diparse.

        IDX mengirim format ISO seperti "2026-07-01T22:19:59". Kita coba beberapa
        format agar tahan terhadap variasi. Dipakai poller untuk high-water-mark
        (hanya alert item yang lebih baru dari yang terakhir diproses).
        """
        raw = (self.published or "").strip()
        if not raw or raw == "-":
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                continue
        # Fallback: coba ISO parser bawaan (menangani offset/mikrodetik).
        # tzinfo dibuang agar selalu naive — high-water-mark membandingkan
        # datetime naive; campuran aware vs naive memicu TypeError.
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            return None

    @classmethod
    def from_idx_row(cls, row: dict) -> "Announcement":
        """Build from one item of the GetAnnouncement `Replies` array.

        The real IDX shape wraps the fields in a `pengumuman` sub-object with a
        sibling `attachments` array::

            {"pengumuman": {"Id2": "...", "Kode_Emiten": "TRIN   ",
                            "JudulPengumuman": "...", "TglPengumuman": "..."},
             "attachments": [{"FullSavePath": "https://.../x.pdf",
                              "PDFFilename": "x.pdf", "IsAttachment": false}]}

        We also accept a flat row (fields at top level) as a fallback so the
        code survives another site reshuffle.
        """
        p = row.get("pengumuman") if isinstance(row.get("pengumuman"), dict) else row

        # `Id` is always 0 in the feed; the stable unique key is Id2 / NoPengumuman.
        ann_id = _first(p, "Id2", "NoPengumuman", "AnnouncementId", "id")
        # Kode_Emiten carries heavy trailing whitespace; _first strips it.
        emiten = _first(p, "Kode_Emiten", "KodeEmiten", "Code", "EmitenCode", "code")
        title = _first(p, "JudulPengumuman", "Title", "Subject", "title")
        published = _first(
            p, "TglPengumuman", "PublishDate", "publishDate", "Date", default="-"
        )

        # Attachments live on the outer row (sibling of `pengumuman`).
        raw_atts = row.get("attachments") or row.get("Attachments") or []
        primary: list[Attachment] = []
        extra: list[Attachment] = []
        for att in raw_atts:
            if not isinstance(att, dict):
                continue
            path = _first(att, "FullSavePath", "PathFile", "FilePath", "fullSavePath", "path")
            if not path:
                continue
            name = (
                _first(att, "OriginalFilename", "PDFFilename", "FileName", "filename")
                or path.rsplit("/", 1)[-1]
            )
            size = att.get("FileSize") or att.get("Size") or att.get("size") or 0
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            item = Attachment(
                filename=name, url=urljoin(IDX_BASE, path.replace("\\", "/")), size=size
            )
            # The main document has IsAttachment=false; lampiran have true.
            if att.get("IsAttachment") is True:
                extra.append(item)
            else:
                primary.append(item)

        return cls(
            id=ann_id,
            emiten=emiten,
            title=title,
            published=published,
            attachments=primary + extra,  # main document(s) first
        )
