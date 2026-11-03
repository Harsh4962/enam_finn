"""Announcement store with dedupe and revision semantics (brief: revisions happen)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .models import Announcement


@dataclass
class IngestOutcome:
    kind: Literal["new", "duplicate", "correction"]
    announcement_id: str
    supersedes: str | None = None


class AnnouncementStore:
    """Keyed by (exchange, announcement_id).

    - Same key, same content: duplicate. Never re-pushed.
    - Same key or revision_of pointing at a stored filing: correction. The record
      updates and the push is labeled a correction, not a fresh alert."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], Announcement] = {}
        self._hashes: dict[tuple[str, str], str] = {}
        self.superseded: set[tuple[str, str]] = set()

    @staticmethod
    def _content_hash(a: Announcement) -> str:
        return hashlib.sha256(f"{a.headline}||{a.body_text}".encode()).hexdigest()

    def ingest(self, ann: Announcement) -> IngestOutcome:
        key = (ann.exchange, ann.announcement_id)
        h = self._content_hash(ann)
        if key in self._records:
            if self._hashes[key] == h:
                return IngestOutcome("duplicate", ann.announcement_id)
            self._records[key] = ann
            self._hashes[key] = h
            return IngestOutcome("correction", ann.announcement_id)
        if ann.revision_of and (ann.exchange, ann.revision_of) in self._records:
            self.superseded.add((ann.exchange, ann.revision_of))
            self._records[key] = ann
            self._hashes[key] = h
            return IngestOutcome("correction", ann.announcement_id, supersedes=ann.revision_of)
        self._records[key] = ann
        self._hashes[key] = h
        return IngestOutcome("new", ann.announcement_id)

    def get(self, exchange: str, announcement_id: str) -> Announcement | None:
        return self._records.get((exchange, announcement_id))

    def __len__(self) -> int:
        return len(self._records)
