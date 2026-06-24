# app/services/research_service.py
# FEAT-album-research-notes Step 4 — service for the writer-facing AI research notes.
#
# This layer ONLY manages album_research row state (enqueue / restart / refine / read).
# The actual research is run out-of-band by the $0 local poller (scripts/research_poller.py),
# which claims `queued` rows and runs headless `claude -p`. In $0 mode there is NO SQS send —
# the INSERT itself is the enqueue. The dormant Lambda path would add a send here.
from __future__ import annotations

import logging
from typing import Optional

from myblog_shared_db.models import Album, AlbumResearch
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Must match scripts/research_poller.py PROMPT_VERSION / the vendored prompt.
PROMPT_VERSION = "v2"


class ResearchStateError(Exception):
    """Invalid research transition (e.g. refine on a non-done row)."""


class AlbumNotFoundError(Exception):
    def __init__(self, album_id: str) -> None:
        super().__init__(album_id)
        self.album_id = album_id


class ResearchService:
    def get_research(self, db: Session, album_id: str) -> Optional[AlbumResearch]:
        return (
            db.query(AlbumResearch)
            .filter(
                AlbumResearch.album_id == album_id,
                AlbumResearch.prompt_version == PROMPT_VERSION,
            )
            .first()
        )

    def status_map(self, db: Session, album_ids) -> dict[str, str]:
        """Batch: {album_id -> research status} for the current PROMPT_VERSION.

        Albums with no research row are absent from the map (the caller renders
        those as "never researched"). One query for the whole bucket tree, mirroring
        BucketService.reviewed_album_ids — so the cover badge can paint the done/
        in-progress dot from the bucket payload without a per-cover GET."""
        ids = {str(a) for a in album_ids}
        if not ids:
            return {}
        rows = (
            db.query(AlbumResearch.album_id, AlbumResearch.status)
            .filter(
                AlbumResearch.album_id.in_(ids),
                AlbumResearch.prompt_version == PROMPT_VERSION,
            )
            .all()
        )
        return {str(album_id): status for album_id, status in rows}

    def _album_exists(self, db: Session, album_id: str) -> bool:
        return db.query(Album.id).filter(Album.id == album_id).first() is not None

    def enqueue_album(self, db: Session, album_id: str) -> bool:
        """Insert a `queued` row for (album, PROMPT_VERSION) iff none exists.

        The UNIQUE(album_id, prompt_version) + ON CONFLICT DO NOTHING is the
        no-duplicate-call invariant: re-adding the same album, button mashing, or
        flipping bucket modes back and forth never produces a second run. Returns
        True iff a new row was inserted. Idempotent and cheap ($0 — no AI call)."""
        stmt = (
            pg_insert(AlbumResearch)
            .values(album_id=album_id, prompt_version=PROMPT_VERSION, status="queued")
            .on_conflict_do_nothing(constraint="uq_album_research_album_prompt")
        )
        result = db.execute(stmt)
        db.commit()
        return bool(result.rowcount)

    def enqueue_bucket(self, db: Session, bucket) -> int:
        """Enqueue the in-scope, note-less items of a bucket per its research_mode.
        'all' = every item; 'selected' = checked items; 'off' = none. Already-noted
        albums are skipped by the enqueue gate (ON CONFLICT). Returns count inserted."""
        mode = bucket.research_mode
        if mode == "all":
            items = list(bucket.items)
        elif mode == "selected":
            items = [it for it in bucket.items if it.research_selected]
        else:
            return 0
        enqueued = 0
        for it in items:
            # FEAT-pocket-buckit Step 6: post-V30 relax a bucket can hold non-album rows
            # (track/review/playback/snapshot, album_id=None). Skip them — else str(None)="None"
            # reaches the NOT-NULL album_research.album_id (a silent partial-enqueue regression:
            # albums after the first non-album row would never be enqueued). add_item guards its
            # own enqueue (buckets.py); this is the bucket-wide path (research_mode flip / reorder).
            if getattr(it, "item_type", "album") != "album" or it.album_id is None:
                continue
            if self.enqueue_album(db, str(it.album_id)):
                enqueued += 1
        return enqueued

    def trigger(
        self,
        db: Session,
        album_id: str,
        *,
        mode: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> AlbumResearch:
        """Manual trigger from any surface (bucket item, /write).

        - no mode: first-time trigger — create a `queued` row if absent, else
          return current state unchanged (a plain POST on an existing row is a
          no-op; the poller already re-claims `failed` rows on its own).
        - 'restart': full redo — reset the row in place (status→queued, note/error/
          tokens/instruction cleared). Previous note replaced.
        - 'refine' (+instruction): incremental — keep the existing note, stash the
          instruction, re-queue. Valid on a `done` row only. A failed refine keeps
          the prior note (this never clears result_md)."""
        if not self._album_exists(db, album_id):
            raise AlbumNotFoundError(album_id)
        row = self.get_research(db, album_id)

        if mode == "refine":
            if row is None or row.status != "done":
                raise ResearchStateError("refine is valid only on a completed note")
            if not instruction or not instruction.strip():
                raise ResearchStateError("refine requires an instruction")
            row.last_instruction = instruction.strip()
            row.refine_count = (row.refine_count or 0) + 1
            row.status = "queued"
            row.error = None
            row.finished_at = None
            # result_md intentionally kept — the poller sends it for an integrated update.
            db.commit()
            db.refresh(row)
            return row

        if mode == "restart":
            if row is None:
                row = AlbumResearch(album_id=album_id, prompt_version=PROMPT_VERSION)
                db.add(row)
            row.status = "queued"
            row.result_md = None
            row.error = None
            row.model = None
            row.tokens_in = None
            row.tokens_out = None
            row.search_count = None
            row.last_instruction = None
            row.started_at = None
            row.finished_at = None
            db.commit()
            db.refresh(row)
            return row

        # No mode → first-time manual trigger. No-op if a row already exists.
        if row is None:
            row = AlbumResearch(
                album_id=album_id, prompt_version=PROMPT_VERSION, status="queued"
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
