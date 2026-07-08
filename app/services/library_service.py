# app/services/library_service.py
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, literal, select, union_all
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
    SpotifyStreamHistory,
    SpotifyTrackPlayEvent,
    Track,
    TrackGenre,
    album_artists_table,
    post_albums_table as post_albums,
    track_artists_table,
)

from app.services.distribution import (
    expand_credits,
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

    FEAT-multi-user Phase 2: the to-listen queue is scoped per user_id (the acting
    member, provisioned at the route). Commit per mutation (mirrors BucketService).
    """

    # ── to-listen: reads ────────────────────────────────────────────────────────

    def list_to_listen(
        self, db: Session, user_id: uuid.UUID
    ) -> List[AlbumToListenItem]:
        return (
            db.query(AlbumToListenItem)
            .options(selectinload(AlbumToListenItem.album).selectinload(Album.artists))
            .filter(AlbumToListenItem.user_id == user_id)
            .order_by(AlbumToListenItem.position, AlbumToListenItem.added_at)
            .all()
        )

    # ── to-listen: mutations ──────────────────────────────────────────────────────

    def add_to_listen(
        self, db: Session, user_id: uuid.UUID, *, album_id: str, note: Optional[str] = None
    ) -> AlbumToListenItem:
        """Append an album to the end of the member's queue. Album must exist; an
        album already in THIS member's queue raises DuplicateItemError
        (UNIQUE(user_id, album_id))."""
        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        exists = (
            db.query(AlbumToListenItem.id)
            .filter(
                AlbumToListenItem.album_id == album_id,
                AlbumToListenItem.user_id == user_id,
            )
            .first()
        )
        if exists is not None:
            raise DuplicateItemError(album_id)

        next_pos = db.execute(
            select(func.coalesce(func.max(AlbumToListenItem.position), -1)).where(
                AlbumToListenItem.user_id == user_id
            )
        ).scalar_one()
        item = AlbumToListenItem(
            user_id=user_id, album_id=album_id, note=note, position=int(next_pos) + 1
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def delete_to_listen(self, db: Session, user_id: uuid.UUID, item_id: str) -> bool:
        item = (
            db.query(AlbumToListenItem)
            .filter(
                AlbumToListenItem.id == item_id,
                AlbumToListenItem.user_id == user_id,
            )
            .first()
        )
        if item is None:
            return False
        db.delete(item)
        db.commit()
        return True

    def reorder_to_listen(
        self, db: Session, user_id: uuid.UUID, item_ids: List[str]
    ) -> None:
        """Rewrite queue positions 0..n from the given top→bottom order. Same
        idempotent mechanism as bucket reorder; an id not in THIS member's queue
        raises ItemNotFoundError."""
        items_by_id = {
            str(it.id): it
            for it in db.query(AlbumToListenItem)
            .filter(
                AlbumToListenItem.id.in_(item_ids),
                AlbumToListenItem.user_id == user_id,
            )
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
            .options(selectinload(SpotifyRecentAlbum.album).selectinload(Album.artists))
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
        performers. The PRIMARY artist source for the saved (좋아요) distribution: it
        captures track-level FEATURES (which album_artists misses) and never collapses
        to the 'Various Artists' album sentinel."""
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
        # fragments solo counts. TRACK-PRIMARY attribution: the track's own performers
        # (track_artists) capture features and never collapse to 'Various Artists';
        # album_artists alone under-counts featured artists (37% of the 좋아요 set are
        # collabs). Falls back to album_artists (non-VA) → split denormalized
        # artist_name (uncatalogued rows). Played stays album_artists (no track grain).
        rows = db.query(
            SpotifySavedTrack.album_id,
            SpotifySavedTrack.track_id,
            SpotifySavedTrack.artist_name,
        ).all()
        track_artist = self._track_artist_names(
            db, {r.track_id for r in rows if r.track_id is not None}
        )
        album_artist = self._album_artist_names(
            db, {r.album_id for r in rows if r.album_id is not None}
        )

        credits = []
        for r in rows:
            names = resolve_saved_artist_names(
                track_artist.get(str(r.track_id)) if r.track_id is not None else None,
                album_artist.get(str(r.album_id)) if r.album_id is not None else None,
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

    # ── 분석 버킷: lifetime stream history (FEAT-listening-history-import Step 4) ──────
    # Ungated count/time top-N over spotify_stream_history (the lifetime GDPR import).
    # SQL GROUP BY count(*) / sum(ms_played) returning top-N — NOT the load-all-rows
    # rank_counts path (the ledger is 100k–500k rows; aggregate in Postgres). These read
    # the DENORMALIZED uri / track_name / artist_name, so they are independent of catalog
    # coverage (the album/era/genre panels are the gated ones — Step 5). The display
    # predicate matches the import defaults: music only (URI prefix, excludes podcasts +
    # local files), ≥30s listened, not skip-throughs. Raw rows are retained; this is a
    # read-time filter. DB-only (hard rule #9).

    _STREAM_MIN_MS = 30_000  # 30s listened — the import display floor

    def _stream_display_filter(self):
        return and_(
            SpotifyStreamHistory.spotify_track_uri.like("spotify:track:%"),
            SpotifyStreamHistory.ms_played >= self._STREAM_MIN_MS,
            SpotifyStreamHistory.skipped.isnot(True),  # NULL + false pass; only true excluded
        )

    # ── 분석 버킷: lifetime + LIVE merge (FEAT-listening-live-merge) ──────────────────
    # The import is authoritative ≤ as_of (max import ts); the live recently-played
    # poller (spotify_track_play_events, worker-written) fills as_of → now. The union is
    # exact by the as_of TIME BOUNDARY — the poller's window is almost all ≤ as_of and
    # drops out, so there is nothing to per-row dedup (the RFC's "never union grains"
    # concern, done safely). Every stream-history aggregation runs over this unified
    # rowset. The live tail is id-only, so its denormalized shape is reconstructed from
    # the catalog and its ms_played is ESTIMATED as track length (no ms in the poller).

    def _unified_events(
        self,
        frm: Optional[datetime] = None,
        to: Optional[datetime] = None,
    ):
        """Per-play subquery: import (≤ as_of, real ms) UNION ALL live (> as_of, ms
        estimated from `tracks.duration_sec`). Columns uri / track_name / artist_name /
        album_id / ms_played / event_ts / src ('import'|'live'). The live tail carries no
        skip/ms display filter (the poller has neither) — a row is a recently-played
        appearance. Aggregate over the returned subquery.

        FEAT-analysis-explore: an optional half-open time window [frm, to) on the per-play
        `event_ts` (import → SSH.ts, live → TPE.played_at), applied to BOTH legs so every
        downstream aggregation re-scopes to the range. `frm` inclusive, `to` exclusive
        (front maps presets → raw timestamps). `as_of` stays the GLOBAL import horizon
        (max ts over the whole import, NOT the ranged subset) so the import/live union
        boundary is invariant under the range — a range entirely ≤ as_of is import-only,
        a range including now still pulls the live tail."""
        SSH = SpotifyStreamHistory
        TPE = SpotifyTrackPlayEvent
        as_of = select(func.max(SSH.ts)).scalar_subquery()

        imp_where = [self._stream_display_filter()]
        if frm is not None:
            imp_where.append(SSH.ts >= frm)
        if to is not None:
            imp_where.append(SSH.ts < to)
        imp = select(
            SSH.spotify_track_uri.label("uri"),
            SSH.track_name.label("track_name"),
            SSH.artist_name.label("artist_name"),
            SSH.album_id.label("album_id"),
            SSH.ms_played.label("ms_played"),
            SSH.ts.label("event_ts"),
            literal("import").label("src"),
        ).where(and_(*imp_where))

        # Live tail: title from the catalog, the album's first artist as album-artist
        # parity, ms ESTIMATED = duration_sec × 1000 (owner decision; overcounts skips /
        # partial plays — surfaced as the `live_streams` honesty caption).
        album_artist = (
            select(Artist.name)
            .select_from(album_artists_table.join(Artist, Artist.id == album_artists_table.c.artist_id))
            .where(album_artists_table.c.album_id == TPE.album_id)
            .order_by(Artist.name)
            .limit(1)
            .scalar_subquery()
        )
        live_where = [TPE.played_at > as_of]
        if frm is not None:
            live_where.append(TPE.played_at >= frm)
        if to is not None:
            live_where.append(TPE.played_at < to)
        live = (
            select(
                (literal("spotify:track:") + TPE.spotify_track_id).label("uri"),
                Track.title.label("track_name"),
                album_artist.label("artist_name"),
                TPE.album_id.label("album_id"),
                (func.coalesce(Track.duration_sec, 0) * 1000).label("ms_played"),
                TPE.played_at.label("event_ts"),
                literal("live").label("src"),
            )
            .select_from(TPE)
            .join(Track, Track.id == TPE.track_id, isouter=True)
            .where(and_(*live_where))
        )
        return union_all(imp, live).subquery("ev")

    def _stream_totals(
        self,
        db: Session,
        frm: Optional[datetime] = None,
        to: Optional[datetime] = None,
    ) -> Dict:
        """Population denominator + the import horizon + the live-tail count, scoped to
        the [frm, to) range when given. `total_streams`/`total_ms`/`live_streams` re-scope
        to the range (so the hero reflects it); `as_of` stays the GLOBAL import horizon
        (the boundary + staleness marker), independent of the range."""
        ev = self._unified_events(frm, to)
        row = db.execute(
            select(
                func.count().label("n"),
                func.coalesce(func.sum(ev.c.ms_played), 0).label("ms"),
                func.count().filter(ev.c.src == "live").label("live_n"),
            ).select_from(ev)
        ).one()
        as_of = db.execute(select(func.max(SpotifyStreamHistory.ts))).scalar()
        return {
            "total_streams": int(row.n),
            "total_ms": int(row.ms),
            "as_of": as_of,
            "live_streams": int(row.live_n or 0),
        }

    def stream_history_top_tracks(
        self, db: Session, *, metric: str = "count", limit: int = 15,
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        # Identity = the track URI (import: real; live: 'spotify:track:'||id); displayed
        # name = the denormalized/reconstructed track_name/artist_name.
        ev = self._unified_events(frm, to)
        plays = func.count().label("plays")
        ms = func.sum(ev.c.ms_played).label("ms")
        primary = ms.desc() if metric == "time" else plays.desc()
        rows = db.execute(
            select(
                ev.c.uri,
                func.max(ev.c.track_name).label("track_name"),
                func.max(ev.c.artist_name).label("artist_name"),
                plays,
                ms,
            )
            .group_by(ev.c.uri)
            .order_by(primary, plays.desc(), ev.c.uri)
            .limit(limit)
        ).all()
        value = (lambda r: int(r.ms)) if metric == "time" else (lambda r: int(r.plays))
        items = [
            {
                "label": r.track_name or "(알 수 없음)",
                "artist": r.artist_name,
                "spotify_track_uri": r.uri,
                "value": value(r),
            }
            for r in rows
        ]
        return {"items": items, "unit": "ms" if metric == "time" else "count", **self._stream_totals(db, frm, to)}

    def stream_history_top_artists(
        self, db: Session, *, metric: str = "count", limit: int = 15,
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        # Artist ranking over the denormalized/reconstructed album-artist name (no
        # per-artist credit split — that needs the catalog FK). Null-artist rows drop out.
        ev = self._unified_events(frm, to)
        plays = func.count().label("plays")
        ms = func.sum(ev.c.ms_played).label("ms")
        primary = ms.desc() if metric == "time" else plays.desc()
        rows = db.execute(
            select(ev.c.artist_name, plays, ms)
            .where(ev.c.artist_name.isnot(None))
            .group_by(ev.c.artist_name)
            .order_by(primary, plays.desc(), ev.c.artist_name)
            .limit(limit)
        ).all()
        value = (lambda r: int(r.ms)) if metric == "time" else (lambda r: int(r.plays))
        items = [{"label": r.artist_name, "value": value(r)} for r in rows]
        return {"items": items, "unit": "ms" if metric == "time" else "count", **self._stream_totals(db, frm, to)}

    # ── 분석 버킷: GATED lifetime panels (FEAT-listening-history-import Step 5) ──────────
    # Album / genre / era / retrospective over the lifetime stream history. GATED on the
    # Step-3 coverage rate (PASSED at 99.7% album / 99.96% release_date). Aggregate by
    # album_id IN SQL first (collapses the 100k-row ledger to ~900 album rows), then
    # resolve genre/era in Python — the same shape as play_events_genre_distribution,
    # NOT a load-all-rows scan. Era buckets Album.release_date (a date, tz-agnostic);
    # retrospective buckets ts AT TIME ZONE 'Asia/Seoul' (export ts is UTC). The
    # residual 미분류 (uncatalogued + ungenred / no-release) is surfaced as a weight for
    # the honesty caption, not hidden. DB-only (rule #9).

    def _stream_album_weights(
        self,
        db: Session,
        metric: str,
        frm: Optional[datetime] = None,
        to: Optional[datetime] = None,
    ) -> Tuple[List[Tuple], int]:
        """[(album_id, weight)] over the unified import+live events WITH a catalog album
        (weight = plays or ms per metric) + the unresolved (no album_id) weight, scoped to
        the [frm, to) range when given."""
        ev = self._unified_events(frm, to)
        plays = func.count().label("plays")
        ms = func.sum(ev.c.ms_played).label("ms")
        rows = db.execute(
            select(ev.c.album_id, plays, ms)
            .where(ev.c.album_id.isnot(None))
            .group_by(ev.c.album_id)
        ).all()
        pick = (lambda r: int(r.ms)) if metric == "time" else (lambda r: int(r.plays))
        weights = [(r.album_id, pick(r)) for r in rows]
        unres = db.execute(
            select(
                func.count().label("c"),
                func.coalesce(func.sum(ev.c.ms_played), 0).label("m"),
            ).select_from(ev).where(ev.c.album_id.is_(None))
        ).one()
        return weights, int(unres.m if metric == "time" else unres.c)

    def stream_history_genre_distribution(
        self, db: Session, *, metric: str = "count",
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        weights, unres = self._stream_album_weights(db, metric, frm, to)
        album_genre = self._album_primary_genre_map(db, {aid for aid, _ in weights})
        labels = [album_genre.get(str(aid)) for aid, _ in weights]  # None = ungenred
        items, unclassified = rank_counts(labels, [w for _, w in weights])
        return {
            "items": [{"label": label, "value": count} for label, count in items],
            "unit": "ms" if metric == "time" else "count",
            "unclassified": unclassified + unres,  # ungenred + uncatalogued
            **self._stream_totals(db, frm, to),
        }

    def stream_history_era_distribution(
        self, db: Session, *, metric: str = "count",
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        weights, unres = self._stream_album_weights(db, metric, frm, to)
        ids = {aid for aid, _ in weights}
        release = (
            {aid: rd for aid, rd in db.execute(
                select(Album.id, Album.release_date).where(Album.id.in_(ids))
            ).all()}
            if ids else {}
        )
        labels: List[Optional[str]] = []
        for aid, _ in weights:
            d = release.get(aid)
            labels.append(f"{(d.year // 10) * 10}s" if d else None)  # None = unknown era
        items, unclassified = rank_counts(labels, [w for _, w in weights])
        items.sort(key=lambda kv: int(kv[0][:-1]))  # chronological by numeric decade ("2020s"→2020), not lexical
        return {
            "items": [{"label": label, "value": count} for label, count in items],
            "unit": "ms" if metric == "time" else "count",
            "unclassified": unclassified + unres,  # no-release + uncatalogued
            **self._stream_totals(db, frm, to),
        }

    def stream_history_top_albums(
        self, db: Session, *, metric: str = "count", limit: int = 15,
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        ev = self._unified_events(frm, to)
        plays = func.count().label("plays")
        ms = func.sum(ev.c.ms_played).label("ms")
        primary = ms.desc() if metric == "time" else plays.desc()
        rows = db.execute(
            select(ev.c.album_id, plays, ms)
            .where(ev.c.album_id.isnot(None))
            .group_by(ev.c.album_id)
            .order_by(primary, plays.desc())
            .limit(limit)
        ).all()
        ids = [r.album_id for r in rows]
        albums = (
            {a.id: a for a in db.query(Album).filter(Album.id.in_(ids))
             .options(selectinload(Album.artists)).all()}
            if ids else {}
        )
        genre_map = GenreService().labels_map(db, [str(i) for i in ids]) if ids else {}
        unres = db.execute(
            select(
                func.count().label("c"),
                func.coalesce(func.sum(ev.c.ms_played), 0).label("m"),
            ).select_from(ev).where(ev.c.album_id.is_(None))
        ).one()
        pick = (lambda r: int(r.ms)) if metric == "time" else (lambda r: int(r.plays))
        items = [
            {"album_obj": albums[r.album_id], "genres": genre_map.get(str(r.album_id), []), "value": pick(r)}
            for r in rows if r.album_id in albums
        ]
        return {
            "items": items,
            "unit": "ms" if metric == "time" else "count",
            "unresolved": int(unres.m if metric == "time" else unres.c),
            **self._stream_totals(db, frm, to),
        }

    def stream_history_retrospective(
        self, db: Session, *, limit: int = 20,
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        ev = self._unified_events(frm, to)
        kst = func.timezone("Asia/Seoul", ev.c.event_ts)
        now_kst = func.timezone("Asia/Seoul", func.now())
        yr = func.extract("year", kst)
        plays = func.count().label("plays")
        ms = func.sum(ev.c.ms_played).label("ms")

        per_year = [
            {"year": int(r.yr), "plays": int(r.plays), "ms_played": int(r.ms)}
            for r in db.execute(
                select(yr.label("yr"), plays, ms)
                .select_from(ev)
                .group_by(yr).order_by(yr)
            ).all()
        ]

        otd_rows = db.execute(
            select(
                yr.label("yr"),
                ev.c.track_name,
                ev.c.artist_name,
                ev.c.album_id,
                plays,
                ms,
            )
            .where(and_(
                func.extract("month", kst) == func.extract("month", now_kst),
                func.extract("day", kst) == func.extract("day", now_kst),
            ))
            .group_by(yr, ev.c.track_name, ev.c.artist_name, ev.c.album_id)
            .order_by(yr.desc(), plays.desc())
            .limit(limit)
        ).all()
        otd_ids = [r.album_id for r in otd_rows if r.album_id is not None]
        otd_albums = (
            {a.id: a for a in db.query(Album).filter(Album.id.in_(otd_ids))
             .options(selectinload(Album.artists)).all()}
            if otd_ids else {}
        )
        otd_genres = GenreService().labels_map(db, [str(i) for i in otd_ids]) if otd_ids else {}
        on_this_day = [
            {
                "year": int(r.yr),
                "track_name": r.track_name,
                "artist_name": r.artist_name,
                "album_id": str(r.album_id) if r.album_id else None,
                "album_obj": otd_albums.get(r.album_id),
                "genres": otd_genres.get(str(r.album_id), []),
                "plays": int(r.plays),
                "ms_played": int(r.ms),
            }
            for r in otd_rows
        ]
        today_kst = db.execute(select(func.to_char(now_kst, "MM-DD"))).scalar()
        totals = self._stream_totals(db, frm, to)
        return {
            "per_year": per_year,
            "on_this_day": on_this_day,
            "today_kst": today_kst,
            "as_of": totals["as_of"],
            "live_streams": totals["live_streams"],
        }

    # ── 분석 버킷: item drill-down + listening clock (FEAT-analysis-explore) ────────────
    # Both re-aggregate the SAME _unified_events union (per-stream event_ts, import+live)
    # with the optional [frm, to) range. Drill-down keys an entity (artist_name | catalog
    # album_id | track uri — the same grouping keys the top-N panels use); the clock is a
    # KST hour×weekday matrix (the retrospective's func.timezone('Asia/Seoul', …) path).
    # DB-only reads (rule #9); count is exact, live-tail time is the same ESTIMATE the rest
    # of the source carries.

    def stream_history_item_detail(
        self, db: Session, *, type_: str, id_: str, metric: str = "count",
        frm: Optional[datetime] = None, to: Optional[datetime] = None, top_limit: int = 10,
    ) -> Dict:
        """One entity's stats over the (optionally ranged) union: count, listening time,
        first/last listen, per-year series, and — for an artist — its top tracks/albums.
        `id_` is the URI (track), the artist_name (artist), or the catalog album_id (album).
        An album id absent from the catalog raises AlbumNotFoundError (→ 404); an entity
        with no plays in range returns a zero detail (not an error)."""
        ev = self._unified_events(frm, to)
        album_uuid = None
        if type_ == "track":
            ent = ev.c.uri == id_
        elif type_ == "artist":
            ent = ev.c.artist_name == id_
        elif type_ == "album":
            try:
                album_uuid = uuid.UUID(str(id_))
            except (ValueError, AttributeError, TypeError):
                raise AlbumNotFoundError(id_)
            ent = ev.c.album_id == album_uuid
        else:
            raise ValueError(f"unknown drill-down type: {type_}")

        plays = func.count().label("plays")
        ms = func.coalesce(func.sum(ev.c.ms_played), 0).label("ms")
        core = db.execute(
            select(
                plays,
                ms,
                func.min(ev.c.event_ts).label("first_listen"),
                func.max(ev.c.event_ts).label("last_listen"),
                func.count().filter(ev.c.src == "live").label("live"),
                func.max(ev.c.track_name).label("track_name"),
                func.max(ev.c.artist_name).label("artist_name"),
            ).select_from(ev).where(ent)
        ).one()

        if type_ == "artist":
            label, artist = id_, None
        elif type_ == "track":
            label, artist = (core.track_name or "(알 수 없음)"), core.artist_name
        else:  # album — resolve the catalog title/artists (id came from the album panel)
            album = (
                db.query(Album).filter(Album.id == album_uuid)
                .options(selectinload(Album.artists)).first()
            )
            if album is None:
                raise AlbumNotFoundError(id_)
            label = album.title
            artist = ", ".join(a.name for a in album.artists if a.name) or None

        kst = func.timezone("Asia/Seoul", ev.c.event_ts)
        yr = func.extract("year", kst)
        per_year = [
            {"year": int(r.yr), "plays": int(r.plays), "ms_played": int(r.ms)}
            for r in db.execute(
                select(yr.label("yr"), plays, ms)
                .select_from(ev).where(ent).group_by(yr).order_by(yr)
            ).all()
        ]

        top_tracks: List[Dict] = []
        top_albums: List[Dict] = []
        if type_ == "artist":
            primary = ms.desc() if metric == "time" else plays.desc()
            pick = (lambda r: int(r.ms)) if metric == "time" else (lambda r: int(r.plays))
            tr = db.execute(
                select(
                    ev.c.uri,
                    func.max(ev.c.track_name).label("track_name"),
                    func.max(ev.c.artist_name).label("artist_name"),
                    plays, ms,
                )
                .select_from(ev).where(ent)
                .group_by(ev.c.uri).order_by(primary, plays.desc(), ev.c.uri).limit(top_limit)
            ).all()
            top_tracks = [
                {"label": r.track_name or "(알 수 없음)", "artist": r.artist_name,
                 "spotify_track_uri": r.uri, "value": pick(r)}
                for r in tr
            ]
            al = db.execute(
                select(ev.c.album_id, plays, ms)
                .select_from(ev).where(and_(ent, ev.c.album_id.isnot(None)))
                .group_by(ev.c.album_id).order_by(primary, plays.desc()).limit(top_limit)
            ).all()
            al_ids = [r.album_id for r in al]
            al_albums = (
                {a.id: a for a in db.query(Album).filter(Album.id.in_(al_ids))
                 .options(selectinload(Album.artists)).all()}
                if al_ids else {}
            )
            al_genres = GenreService().labels_map(db, [str(i) for i in al_ids]) if al_ids else {}
            top_albums = [
                {"album_obj": al_albums[r.album_id], "genres": al_genres.get(str(r.album_id), []), "value": pick(r)}
                for r in al if r.album_id in al_albums
            ]

        as_of = db.execute(select(func.max(SpotifyStreamHistory.ts))).scalar()
        return {
            "type": type_,
            "id": id_,
            "label": label,
            "artist": artist,
            "unit": "ms" if metric == "time" else "count",
            "count": int(core.plays),
            "time_ms": int(core.ms),
            "first_listen": core.first_listen,
            "last_listen": core.last_listen,
            "per_year": per_year,
            "top_tracks": top_tracks,
            "top_albums": top_albums,
            "as_of": as_of,
            "live_streams": int(core.live or 0),
        }

    def stream_history_clock(
        self, db: Session, *, metric: str = "count",
        frm: Optional[datetime] = None, to: Optional[datetime] = None,
    ) -> Dict:
        """Hour-of-day × weekday matrix over the (optionally ranged) union, in Asia/Seoul.
        Returns only the non-empty cells (≤168). `weekday` = Postgres extract(dow): 0=Sun
        … 6=Sat. `metric` selects whether the front colours by plays or ms (both shipped)."""
        ev = self._unified_events(frm, to)
        kst = func.timezone("Asia/Seoul", ev.c.event_ts)
        dow = func.extract("dow", kst)
        hour = func.extract("hour", kst)
        plays = func.count().label("plays")
        ms = func.coalesce(func.sum(ev.c.ms_played), 0).label("ms")
        rows = db.execute(
            select(dow.label("dow"), hour.label("hour"), plays, ms)
            .select_from(ev).group_by(dow, hour)
        ).all()
        cells = [
            {"weekday": int(r.dow), "hour": int(r.hour), "plays": int(r.plays), "ms_played": int(r.ms)}
            for r in rows
        ]
        return {
            "cells": cells,
            "unit": "ms" if metric == "time" else "count",
            **self._stream_totals(db, frm, to),
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
