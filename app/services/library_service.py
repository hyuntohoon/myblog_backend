# app/services/library_service.py
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from myblog_shared_db.models import (
    Album,
    AlbumToListenItem,
    Artist,
    Genre,
    Post,
    SpotifyNowPlaying,
    SpotifyPlayEvent,
    SpotifyRecentAlbum,
    SpotifyRecentTrack,
    SpotifySavedTrack,
    TrackGenre,
    post_albums_table as post_albums,
    track_artists_table,
)

from app.services.distribution import (
    expand_credits,
    is_va_compilation,
    rank_counts,
    resolve_saved_artist_names,
)
from app.services.genre_service import GenreService


class AlbumNotFoundError(Exception):
    """Raised when an album id does not exist. Route maps to 404."""


class ItemNotFoundError(Exception):
    """Raised when a to-listen item id does not exist. Route maps to 404."""


class DuplicateItemError(Exception):
    """Raised when an album is already in the to-listen queue. Route maps to 409."""


class LibraryService:
    """Member-dashboard Library tab (FEAT-member-dashboard Step 2, D18).

    Two of the three Library sources live here:
      - 들을 것 (to-listen): a manual, position-ordered queue (album_to_listen_items).
      - 평론한 앨범 (reviewed): a read-only view derived from published posts
        (post_albums ⋈ posts where status='published'), grouped by album.
    "최근 들은 앨범" (Spotify cache) is Step 3, not here.

    Single user → no user_id / ownership checks. Commit per mutation (mirrors
    BucketService).
    """

    # ── to-listen: reads ────────────────────────────────────────────────────────

    def list_to_listen(self, db: Session) -> List[AlbumToListenItem]:
        return (
            db.query(AlbumToListenItem)
            .order_by(AlbumToListenItem.position, AlbumToListenItem.added_at)
            .all()
        )

    # ── to-listen: mutations ──────────────────────────────────────────────────────

    def add_to_listen(
        self, db: Session, *, album_id: str, note: Optional[str] = None
    ) -> AlbumToListenItem:
        """Append an album to the end of the queue. Album must exist; an album
        already queued raises DuplicateItemError (UNIQUE album_id)."""
        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        exists = (
            db.query(AlbumToListenItem.id)
            .filter(AlbumToListenItem.album_id == album_id)
            .first()
        )
        if exists is not None:
            raise DuplicateItemError(album_id)

        next_pos = db.execute(
            select(func.coalesce(func.max(AlbumToListenItem.position), -1))
        ).scalar_one()
        item = AlbumToListenItem(
            album_id=album_id, note=note, position=int(next_pos) + 1
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def delete_to_listen(self, db: Session, item_id: str) -> bool:
        item = (
            db.query(AlbumToListenItem)
            .filter(AlbumToListenItem.id == item_id)
            .first()
        )
        if item is None:
            return False
        db.delete(item)
        db.commit()
        return True

    def reorder_to_listen(self, db: Session, item_ids: List[str]) -> None:
        """Rewrite queue positions 0..n from the given top→bottom order. Same
        idempotent mechanism as bucket reorder; an unknown id raises
        ItemNotFoundError."""
        items_by_id = {
            str(it.id): it
            for it in db.query(AlbumToListenItem)
            .filter(AlbumToListenItem.id.in_(item_ids))
            .all()
        }
        unknown = [iid for iid in item_ids if str(iid) not in items_by_id]
        if unknown:
            raise ItemNotFoundError(unknown[0])

        for pos, item_id in enumerate(item_ids):
            items_by_id[str(item_id)].position = pos
        db.commit()

    # ── reviewed: derived view ────────────────────────────────────────────────────

    def list_reviewed(self, db: Session) -> List[Tuple[Album, List[str]]]:
        """One entry per album that has ≥1 published review, with the album's
        published post ids. Albums ordered by most-recent review first.

        Derived from post_albums ⋈ posts(status='published') — no table. The
        album↔review M:N is preserved (review_ids is a list).
        """
        rows = db.execute(
            select(post_albums.c.album_id, Post.id, Post.posted_date)
            .join(Post, Post.id == post_albums.c.post_id)
            .where(Post.status == "published")
            .order_by(Post.posted_date.desc())
        ).all()

        review_ids: Dict[str, List[str]] = {}
        latest: Dict[str, date] = {}
        for album_id, post_id, posted_date in rows:
            aid = str(album_id)
            review_ids.setdefault(aid, []).append(str(post_id))
            if aid not in latest:
                latest[aid] = posted_date  # first seen = newest (rows are desc)

        if not review_ids:
            return []

        albums = {
            str(a.id): a
            for a in db.query(Album).filter(Album.id.in_(list(review_ids))).all()
        }
        ordered = sorted(
            (aid for aid in review_ids if aid in albums),
            key=lambda aid: latest[aid],
            reverse=True,
        )
        return [(albums[aid], review_ids[aid]) for aid in ordered]

    # ── 최근 들은 앨범 + now-playing: read-only Spotify cache (Step 3, D25/D5) ────────
    # Populated by the worker (EventBridge cron + manual SQS refresh). These reads
    # never touch Spotify (hard rule #9).

    def list_recently_listened(self, db: Session) -> List[Tuple[Album, datetime]]:
        """The distinct recently-played album set, most-recently-played first.
        Returns (album, last_played_at) pairs."""
        rows = (
            db.query(SpotifyRecentAlbum)
            .order_by(SpotifyRecentAlbum.last_played_at.desc())
            .all()
        )
        return [(r.album, r.last_played_at) for r in rows if r.album is not None]

    def last_recent_synced_at(self, db: Session) -> Optional[datetime]:
        """When the worker last wrote the recently-listened cache (max synced_at),
        or None when empty. The UI polls this after a manual refresh (D31)."""
        return db.query(func.max(SpotifyRecentAlbum.synced_at)).scalar()

    def get_now_playing(self, db: Session) -> Optional[SpotifyNowPlaying]:
        """The single-row now-playing cache (id=1), or None if never synced."""
        return (
            db.query(SpotifyNowPlaying)
            .filter(SpotifyNowPlaying.id == 1)
            .first()
        )

    # ── 최근 재생 트랙 + 들은 앨범(누적): durable data (FEAT-member-dashboard-realdata) ──
    # recent-tracks is the worker-fed rolling cache (D-B); listened-albums aggregates
    # the append-only spotify_play_events log (D-A, no rollup table). Neither calls
    # Spotify (hard rule #9).

    def list_recent_tracks(self, db: Session) -> List[SpotifyRecentTrack]:
        """One row per distinct track (its most-recent play), most-recently-played
        first. The cache stores one row per play event (UNIQUE(spotify_track_id,
        played_at)), so a looped/repeated track would otherwise appear N times
        (item 10). Rows carry denormalized track/artist/album text; `.album` is set
        only when the track's album is in our catalog (album_id FK). Row 0 is the
        "최근 재생" latest play."""
        # DISTINCT ON keeps the latest row per track (orders by track id first);
        # the window is small (<=50), so re-sort to played_at DESC in Python.
        rows = (
            db.query(SpotifyRecentTrack)
            .distinct(SpotifyRecentTrack.spotify_track_id)
            .order_by(
                SpotifyRecentTrack.spotify_track_id,
                SpotifyRecentTrack.played_at.desc(),
            )
            .all()
        )
        rows.sort(key=lambda r: r.played_at, reverse=True)
        return rows

    def last_recent_tracks_synced_at(self, db: Session) -> Optional[datetime]:
        """When the worker last wrote the recent-tracks cache (max synced_at)."""
        return db.query(func.max(SpotifyRecentTrack.synced_at)).scalar()

    def list_listened_albums(
        self, db: Session, *, limit: int = 200, offset: int = 0
    ) -> Tuple[List[Tuple[Album, int, datetime, datetime]], int]:
        """The cumulative listened-album archive, derived (D-A) from the append-only
        spotify_play_events log — no rollup table. One entry per album with its
        play_count + first/last play, most-recently-played first, paginated.

        Returns ((album, play_count, first_played_at, last_played_at)…, total) where
        total is the distinct-album count (for pagination / a "N albums" stat).
        """
        agg = (
            select(
                SpotifyPlayEvent.album_id.label("album_id"),
                func.count().label("play_count"),
                func.min(SpotifyPlayEvent.played_at).label("first_played_at"),
                func.max(SpotifyPlayEvent.played_at).label("last_played_at"),
            )
            .group_by(SpotifyPlayEvent.album_id)
            .subquery()
        )
        total = db.execute(select(func.count()).select_from(agg)).scalar_one()
        rows = db.execute(
            select(Album, agg.c.play_count, agg.c.first_played_at, agg.c.last_played_at)
            .join(Album, Album.id == agg.c.album_id)
            .order_by(agg.c.last_played_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows], int(total)

    # ── 분석 버킷: saved tracks (좋아요) + genre/artist distribution ──────────────────
    # FEAT-genre-artist-distribution. The /profile 분석 버킷 lists the owner's Spotify
    # 좋아요 tracks (worker-fed spotify_saved_tracks cache) and renders genre/artist
    # distributions. A SECOND co-equal source — play history (spotify_play_events) —
    # feeds the SAME shared rank_counts via one response shape so the front chart is
    # source-agnostic. Genre resolves track_genres(override) → album_genres(inherit)
    # → else 미분류. All DB-only reads (hard rule #9).

    def list_saved_tracks(
        self, db: Session, *, limit: int = 200, offset: int = 0
    ) -> Tuple[List[SpotifySavedTrack], int, Optional[datetime]]:
        """Paginated saved-tracks list (most-recently-liked first) + total +
        last_synced_at. Each row carries denormalized text columns; `.album` (and its
        `.artists`) are eager-loaded so the route can build an AlbumBrief per row
        without an N+1 — at the workbench's limit=500 the per-row lazy loads timed out
        the Lambda (the old UI only fetched 60). Mirrors the played-albums eager-load."""
        total = db.query(func.count()).select_from(SpotifySavedTrack).scalar() or 0
        last_synced = db.query(func.max(SpotifySavedTrack.synced_at)).scalar()
        rows = (
            db.query(SpotifySavedTrack)
            .options(selectinload(SpotifySavedTrack.album).selectinload(Album.artists))
            .order_by(SpotifySavedTrack.added_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return rows, int(total), last_synced

    def _album_primary_genre_map(self, db: Session, album_ids) -> Dict[str, str]:
        """{album_id_str -> primary genre label} via the canonical album-genre
        resolution (prefer-high-else-low, ordered by display position; [0] is the
        album's primary genre). Reuses GenreService.labels_map for one source of
        truth on the high/low-confidence rule."""
        labels = GenreService().labels_map(db, album_ids)
        return {aid: lbls[0] for aid, lbls in labels.items() if lbls}

    def _track_override_genre_map(self, db: Session, track_ids) -> Dict[str, str]:
        """{track_id_str -> primary override genre label} from track_genres (an
        override-only table — a row exists iff the track deviates from its album).
        Ordered by the genre's display position so the first seen is the primary
        override."""
        ids = {str(t) for t in track_ids if t is not None}
        if not ids:
            return {}
        rows = (
            db.query(TrackGenre.track_id, Genre.label)
            .join(Genre, Genre.id == TrackGenre.genre_id)
            .filter(TrackGenre.track_id.in_(ids))
            .order_by(Genre.position)
            .all()
        )
        out: Dict[str, str] = {}
        for track_id, label in rows:
            out.setdefault(str(track_id), label)  # first = lowest position = primary
        return out

    def _resolve_saved_genre(self, row, override_map, album_genre_map) -> Optional[str]:
        """Effective genre for a saved track: track_genres override → album_genres
        inherit → None (미분류)."""
        if row.track_id is not None:
            g = override_map.get(str(row.track_id))
            if g is not None:
                return g
        if row.album_id is not None:
            return album_genre_map.get(str(row.album_id))
        return None

    def saved_tracks_genre_distribution(self, db: Session) -> Dict:
        rows = db.query(
            SpotifySavedTrack.track_id,
            SpotifySavedTrack.album_id,
        ).all()
        album_ids = {r.album_id for r in rows if r.album_id is not None}
        track_ids = {r.track_id for r in rows if r.track_id is not None}
        album_genre = self._album_primary_genre_map(db, album_ids)
        override = self._track_override_genre_map(db, track_ids)

        labels: List[Optional[str]] = []
        uncatalogued = 0
        ungenred = 0
        for r in rows:
            g = self._resolve_saved_genre(r, override, album_genre)
            labels.append(g)
            if g is None:
                if r.album_id is None:
                    uncatalogued += 1
                else:
                    ungenred += 1
        items, unclassified = rank_counts(labels)
        return {
            "items": items,
            "unclassified_count": unclassified,
            "total": len(rows),
            "uncatalogued": uncatalogued,
            "ungenred": ungenred,
            "last_synced_at": db.query(func.max(SpotifySavedTrack.synced_at)).scalar(),
        }

    def _album_artist_names(self, db: Session, album_ids) -> Dict[str, List[str]]:
        """{album_id_str -> [artist name, …]} via the album_artists join — one row
        per artist with the name intact. The exact, comma-safe source for artist
        attribution, preferred over splitting the denormalized artist_name whenever
        an album_id is present. Empty list when the album has no catalogued artists.
        Eager-loads Album.artists to avoid an N+1 over the distinct albums."""
        out: Dict[str, List[str]] = {}
        ids = {a for a in album_ids if a is not None}
        if not ids:
            return out
        albums = (
            db.query(Album)
            .filter(Album.id.in_(ids))
            .options(selectinload(Album.artists))
            .all()
        )
        for a in albums:
            out[str(a.id)] = [ar.name for ar in a.artists if ar.name]
        return out

    def _track_artist_names(self, db: Session, track_ids) -> Dict[str, List[str]]:
        """{track_id_str -> [artist name, …]} via the track_artists join — the per-track
        performers. Used as the saved-track fallback when the album is a Various-Artists
        compilation, whose album_artists credit ('Various Artists') would otherwise hide
        the real performer."""
        out: Dict[str, List[str]] = {}
        ids = {t for t in track_ids if t is not None}
        if not ids:
            return out
        rows = (
            db.query(track_artists_table.c.track_id, Artist.name)
            .join(Artist, Artist.id == track_artists_table.c.artist_id)
            .filter(track_artists_table.c.track_id.in_(ids))
            .all()
        )
        for track_id, name in rows:
            if name:
                out.setdefault(str(track_id), []).append(name)
        return out

    def saved_tracks_artist_distribution(self, db: Session) -> Dict:
        # Per-artist credit (FIX-analysis-artist-attribution): a collab is split into
        # one credit per artist instead of a single comma-joined 'A, B' bucket that
        # fragments solo counts. Attribution is album_artists (exact) by default; a
        # Various-Artists compilation falls back to the track's own artists (the album
        # credit would hide the real performer), then to splitting the denormalized
        # artist_name (also the path for uncatalogued rows — currently empty in prod).
        rows = db.query(
            SpotifySavedTrack.album_id,
            SpotifySavedTrack.track_id,
            SpotifySavedTrack.artist_name,
        ).all()
        album_artist = self._album_artist_names(
            db, {r.album_id for r in rows if r.album_id is not None}
        )
        va_albums = {aid for aid, names in album_artist.items() if is_va_compilation(names)}
        va_track_ids = {
            r.track_id
            for r in rows
            if r.track_id is not None
            and r.album_id is not None
            and str(r.album_id) in va_albums
        }
        track_artist = self._track_artist_names(db, va_track_ids)

        credits = []
        for r in rows:
            aid = str(r.album_id) if r.album_id is not None else None
            names = resolve_saved_artist_names(
                album_artist.get(aid) if aid else None,
                track_artist.get(str(r.track_id)) if r.track_id is not None else None,
                r.artist_name,
            )
            credits.append((names, 1))
        labels, weights = expand_credits(credits)
        items, unclassified = rank_counts(labels, weights)
        return {
            "items": items,
            "unclassified_count": unclassified,
            # total stays the true population (liked track count); per-artist
            # credits make sum(items) exceed (total - unclassified) for collabs.
            "total": len(rows),
            "uncatalogued": None,  # breakdown is genre-only
            "ungenred": None,
            "last_synced_at": db.query(func.max(SpotifySavedTrack.synced_at)).scalar(),
        }

    # ── 분석 버킷: play-history source (spotify_play_events) — same shared module ─────

    def _play_event_album_counts(self, db: Session):
        """[(album_id, play_count)] over the append-only play-events log."""
        return db.execute(
            select(SpotifyPlayEvent.album_id, func.count().label("c"))
            .group_by(SpotifyPlayEvent.album_id)
        ).all()

    def play_events_genre_distribution(self, db: Session) -> Dict:
        rows = self._play_event_album_counts(db)
        album_ids = {r.album_id for r in rows}
        album_genre = self._album_primary_genre_map(db, album_ids)

        labels: List[Optional[str]] = []
        weights: List[int] = []
        ungenred = 0
        for r in rows:
            g = album_genre.get(str(r.album_id))
            labels.append(g)
            weights.append(int(r.c))
            if g is None:
                ungenred += int(r.c)
        items, unclassified = rank_counts(labels, weights)
        return {
            "items": items,
            "unclassified_count": unclassified,
            "total": sum(int(r.c) for r in rows),
            "uncatalogued": 0,  # spotify_play_events.album_id is NOT NULL → catalog-present
            "ungenred": ungenred,
            "last_synced_at": db.query(func.max(SpotifyPlayEvent.played_at)).scalar(),
        }

    def play_events_artist_distribution(self, db: Session) -> Dict:
        # Per-artist credit (FIX-analysis-artist-attribution): each artist of a
        # played album gets the album's full play_count, instead of crediting one
        # comma-joined 'A, B' label. album_id is NOT NULL here, so the album_artists
        # join is always the source (no denormalized fallback). An album with no
        # catalogued artists weights into unclassified by its play_count.
        rows = self._play_event_album_counts(db)
        album_artist = self._album_artist_names(db, {r.album_id for r in rows})

        labels, weights = expand_credits(
            (album_artist.get(str(r.album_id)), int(r.c)) for r in rows
        )
        items, unclassified = rank_counts(labels, weights)
        return {
            "items": items,
            "unclassified_count": unclassified,
            "total": sum(int(r.c) for r in rows),
            "uncatalogued": None,
            "ungenred": None,
            "last_synced_at": db.query(func.max(SpotifyPlayEvent.played_at)).scalar(),
        }

    # ── 분석 버킷: 분류하기 targets ───────────────────────────────────────────────────

    def unclassified_saved_album_targets(
        self, db: Session
    ) -> Tuple[List[str], int]:
        """For 분류하기: split the UNCLASSIFIED saved tracks into actionable groups.

        Returns (uncatalogued_sids, needs_backfill_count) where:
          - uncatalogued_sids = distinct spotify album ids NOT in our catalog
            (album_id NULL) — the genuinely enqueueable set (→ catalog sync → S1
            genres → the track inherits a genre).
          - needs_backfill_count = catalog-present-but-ungenred saved tracks; these
            can't be fixed by re-syncing (their artists carry no Spotify genres) — they
            need the manual iTunes backfill, so 분류하기 reports rather than enqueues them.
        """
        rows = db.query(
            SpotifySavedTrack.album_sid,
            SpotifySavedTrack.album_id,
            SpotifySavedTrack.track_id,
        ).all()
        album_ids = {r.album_id for r in rows if r.album_id is not None}
        track_ids = {r.track_id for r in rows if r.track_id is not None}
        album_genre = self._album_primary_genre_map(db, album_ids)
        override = self._track_override_genre_map(db, track_ids)

        uncatalogued_sids: set[str] = set()
        needs_backfill = 0
        for r in rows:
            if self._resolve_saved_genre(r, override, album_genre) is not None:
                continue  # already classified
            if r.album_id is None:
                if r.album_sid:
                    uncatalogued_sids.add(r.album_sid)
            else:
                needs_backfill += 1
        return sorted(uncatalogued_sids), needs_backfill
