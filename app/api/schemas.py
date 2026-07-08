# app/api/schemas.py
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal, Dict, Any
from datetime import date, datetime
from uuid import UUID


# ====== Posts ======

class WritePostRequest(BaseModel):
    model_config = {
        "populate_by_name": True,
        "extra": "ignore",
    }

    title: str = Field(min_length=1)
    body_mdx: Optional[str] = None  # 필수 → 선택 (평점-only 허용)
    description: str = ""
    posted_date: date = Field(default_factory=date.today)
    status: Literal["draft", "published", "archived"] = "published"

    category: Optional[str] = None
    search_index: Optional[bool] = Field(default=None)  # None이면 서비스에서 자동 결정

    # STAB-5 Step 4: review tags (cross-cutting M:N). List of seeded tag *names*;
    # unknown names are rejected (400). Empty list / omitted = no tags.
    tags: List[str] = Field(default_factory=list)

    album_ids: List[str] = Field(default_factory=list)
    artist_ids: List[str] = Field(default_factory=list)

    # FEAT-genre-subgenres: editorial tier-1 sub-genre tags — list of genre *ids*
    # (from the /genres tree palette; unknown ids rejected, 400). The HUMAN layer,
    # distinct from machine album_genres. Empty list / omitted = no tags.
    genre_ids: List[str] = Field(default_factory=list)

    # 평점
    rating: Optional[float] = Field(default=None, ge=0, le=5)

    # 앨범별 명반 여부
    album_classics: Dict[str, bool] = Field(default_factory=dict)
    # 예: {"album-uuid-1": true, "album-uuid-2": false}

    # FEAT-writer-lowfreq-redesign Step 5: writer's BEST NEW MUSIC toggle.
    # When non-null AND exactly one album_ids entry, the service UPDATEs
    # albums.best_new in the same transaction as the post insert/update.
    # Null = "don't touch" (Writer omits the field when no subject is set).
    subject_best_new: Optional[bool] = None

    # 추천 트랙 — FEAT-view-redesign Step 3: set of picked track IDs
    # (no order, no per-row position/note). Album linkage is resolved from
    # `tracks.album_id` server-side; album must be in `album_ids`.
    recommended_track_ids: List[str] = Field(default_factory=list)


class WritePostResponse(BaseModel):
    id: str
    slug: str


class PostListItem(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    slug: str
    title: str
    description: str
    status: str
    posted_date: date
    rating: Optional[float]
    category: Optional[str] = None
    # FEAT-member-dashboard-realdata Step 3: album cover for the /profile draft card
    # (Post.album_cover_url). Lets a draft review render a cover without a second fetch.
    album_cover_url: Optional[str] = None
    # STAB-5 Step 4: review tag names attached to the post (admin list view).
    tags: List[str] = Field(default_factory=list)


class PostListResponse(BaseModel):
    posts: List[PostListItem]


class UpdatePostRequest(BaseModel):
    model_config = {"extra": "ignore"}

    title: Optional[str] = None
    description: Optional[str] = None
    body_mdx: Optional[str] = None
    posted_date: Optional[date] = None
    status: Optional[Literal["draft", "published", "archived"]] = None
    rating: Optional[float] = Field(default=None, ge=0, le=5)

    # BUG-10: editing a draft must be able to change category + linked
    # albums/artists, not just scalar text fields. None on these means "not
    # provided in this request" (no change); to clear, pass an empty list /
    # empty string. exclude_unset on the route side preserves that distinction.
    category: Optional[str] = None
    album_ids: Optional[List[str]] = None
    artist_ids: Optional[List[str]] = None
    # STAB-5 Step 4: review tag names. Empty list = explicit clear; None = "not
    # provided" (no change). exclude_unset on the route preserves the distinction.
    tags: Optional[List[str]] = None
    # FEAT-genre-subgenres: editorial sub-genre tag ids. Empty list = explicit
    # clear; None = "not provided" (no change). exclude_unset preserves the distinction.
    genre_ids: Optional[List[str]] = None
    # Same shape as create — set of track IDs (no position/note).
    # Empty list = explicit clear; None = "not provided" (no change).
    recommended_track_ids: Optional[List[str]] = None
    # FEAT-writer-lowfreq-redesign Step 5: same semantics as on create — null
    # means "no change," non-null triggers the album-level UPDATE.
    subject_best_new: Optional[bool] = None


class PostDetailResponse(BaseModel):
    id: str
    slug: str
    title: str
    description: str
    body_mdx: Optional[str]
    status: str
    posted_date: date
    rating: Optional[float]
    category: Optional[str]
    album_ids: List[str]
    artist_ids: List[str]
    # STAB-5 Step 4: review tag names (seeds the writer's tag picker on edit).
    tags: List[str] = Field(default_factory=list)
    # FEAT-genre-subgenres: editorial sub-genre tag ids (seeds the writer's genre picker on edit).
    genre_ids: List[str] = Field(default_factory=list)
    recommended_track_ids: List[str] = Field(default_factory=list)
    # FEAT-writer-lowfreq-redesign Step 5: joined from the post's subject album
    # so the writer's edit flow can seed the BEST NEW pill on load. Null when
    # the post has zero or many albums (no single subject to read from).
    subject_best_new: Optional[bool] = None


# ====== Review buckets (FEAT-review-bucket-board) ======

class CreateBucketRequest(BaseModel):
    name: str = Field(min_length=1)
    color: Optional[str] = None
    # FEAT-my-buckit-artist (V32): the bucket-level type discriminator. 'general' (default,
    # today's polymorphic membership — every existing caller unchanged) | 'artist' (accepts
    # artist members only). Set once at create; immutable in v1 (see update_bucket guard).
    type: Literal["general", "artist"] = "general"


class UpdateBucketRequest(BaseModel):
    model_config = {"extra": "ignore"}

    # All optional; exclude_unset on the route distinguishes "not provided" from
    # an explicit clear. is_done toggles the single "작성 완료" column.
    name: Optional[str] = None
    color: Optional[str] = None
    position: Optional[int] = None
    is_done: Optional[bool] = None
    # FEAT-album-research-notes: opt-in auto-research scope for this bucket. A PATCH
    # to 'all'/'selected' enqueues note-less in-scope items (fire-and-forget).
    research_mode: Optional[Literal["off", "all", "selected"]] = None
    # FEAT-public-bucket-multiuser Scope A: opt-in public visibility. When true this
    # bucket appears in the public read-only viewer (GET /api/buckets/public). Default
    # false (private); the owner toggles it per bucket. spotify_library is never public.
    is_public: Optional[bool] = None


class SnapshotCaptureRequest(BaseModel):
    """FEAT-pocket-buckit Step 6 (OQ7) — the frozen capture for a snapshot-kind membership.
    The caller (an analysis/period drop) sends the contemporaneous values it is displaying;
    the server APPEND-ONLY INSERTs them into bucket_item_snapshots (never an UPDATE — a
    refresh is a new row). `frozen` is the verbatim payload; the typed header columns drive
    later querying. captured_at + schema_version are server-stamped."""
    kind: str = Field(min_length=1)            # e.g. 'period' | 'signal' | 'trend'
    as_of: datetime                            # the data horizon the snapshot reflects
    frozen: Dict[str, Any]                     # verbatim captured payload (never reinterpreted)
    metric: Optional[str] = None
    range_from: Optional[datetime] = None
    range_to: Optional[datetime] = None
    unit: Optional[str] = None
    total: Optional[float] = None
    unresolved: int = 0
    unclassified: int = 0
    # Typed as UUID so a malformed id is a clean 422 at request validation rather than an
    # uncaught ValueError -> 500 in the snapshot capture.
    source_album_ids: List[UUID] = Field(default_factory=list)


class AddBucketItemRequest(BaseModel):
    # FEAT-pocket-buckit: generalized typed membership — the STEP-2 relax (V30/V31) is live, so
    # non-album writes are now enabled. item_type defaults 'album' so every existing caller is
    # unchanged; each kind carries its own typed target, validated below. album_id is now
    # optional (only album rows carry it).
    item_type: Literal["album", "track", "review", "playback", "snapshot", "artist"] = "album"
    album_id: Optional[str] = None             # required for item_type='album'
    track_id: Optional[str] = None             # required for item_type in {'track','playback'}
    review_target_id: Optional[str] = None     # required for item_type='review'
    note: Optional[str] = None
    snapshot: Optional[SnapshotCaptureRequest] = None  # required for item_type='snapshot'
    # FEAT-my-buckit-artist (V32): item_type='artist' carries EITHER a direct artist_id (add
    # one known artist) OR exactly one source_* (a featuring track / compilation album the
    # server EXPANDS into its credited artists — the source itself is never stored). The
    # validator below enforces the either/or.
    artist_id: Optional[str] = None            # a direct artist add (item_type='artist')
    source_album_id: Optional[str] = None      # expand this album's credited artists
    source_track_id: Optional[str] = None      # expand this track's credited artists

    @model_validator(mode="after")
    def _require_typed_target(self) -> "AddBucketItemRequest":
        # Reject a write that names a kind but omits its typed target up front (422) rather
        # than leaking a downstream IntegrityError / NOT-NULL violation.
        if self.item_type == "artist":
            # Exactly one of {artist_id, source_album_id, source_track_id}. A direct add names
            # the artist; a source_* triggers server-side expansion (mutually exclusive).
            provided = [
                v for v in (self.artist_id, self.source_album_id, self.source_track_id) if v
            ]
            if len(provided) != 1:
                raise ValueError(
                    "artist item requires exactly one of artist_id / source_album_id / "
                    "source_track_id"
                )
            return self
        required = {
            "album": self.album_id,
            "track": self.track_id,
            "playback": self.track_id,
            "review": self.review_target_id,
            "snapshot": self.snapshot,
        }[self.item_type]
        if not required:
            raise ValueError(f"{self.item_type} item requires its typed target")
        return self


class UpdateBucketItemRequest(BaseModel):
    model_config = {"extra": "ignore"}

    note: Optional[str] = None
    status: Optional[Literal["candidate", "drafting", "published"]] = None
    post_id: Optional[str] = None
    # FEAT-album-research-notes: per-item checkbox, meaningful only while the parent
    # bucket's research_mode='selected'. Checking it (in 'selected' mode) enqueues.
    research_selected: Optional[bool] = None
    # FEAT-editor-buckit Stage 1: the "오늘 밤 키우기" gate. When true, the nightly $0
    # memo→skeleton job is cleared to process this item's `note` memo. Write-only here;
    # no enqueue/side-effect (the offline job reads it, the API just stores it).
    prep_tonight: Optional[bool] = None


class ReorderBucket(BaseModel):
    id: str
    item_ids: List[str] = Field(default_factory=list)


class ReorderRequest(BaseModel):
    buckets: List[ReorderBucket] = Field(default_factory=list)


class AlbumBrief(BaseModel):
    id: str
    title: str
    cover_url: Optional[str] = None
    release_date: Optional[date] = None
    popularity: Optional[int] = None
    artist_names: List[str] = Field(default_factory=list)
    # FEAT-bucket-organize Step 2: high-confidence tier-0 genre labels (album_genres
    # join), ordered by the genre's display position so element [0] is the album's
    # primary genre (the single "home" for the crate's group-by-genre, OQ5); the full
    # list drives the genre filter. HIGH-confidence only (OQ4) — empty when the album
    # has no high-confidence rows. Additive + front-graceful.
    genres: List[str] = Field(default_factory=list)


class TrackBrief(BaseModel):
    """FEAT-pocket-buckit Step 6: minimal track display for a track/playback membership row,
    so the front can render the track without a second resolve. The cover lives on the album
    (album_id is provided → the front reuses its album-cover path)."""
    id: str
    title: str
    album_id: Optional[str] = None
    artist_names: List[str] = Field(default_factory=list)
    duration_sec: Optional[int] = None


class ArtistBrief(BaseModel):
    """FEAT-my-buckit-artist (V32): minimal artist display for an artist-kind membership row
    (Artist Buckit member) and for the expansion added/skipped lists. Member click-through
    targets the existing /artist/[id] page (no new detail modal)."""
    id: str
    name: str
    photo_url: Optional[str] = None


class BucketItemExpansion(BaseModel):
    """FEAT-my-buckit-artist (V32): the result of a source_* artist add — a featuring track /
    compilation album expanded into its credited artists (Various Artists excluded). Present
    ONLY on the add response for a source_* artist add; the source row itself is never stored.
    `added` = artists newly inserted this call; `skipped` = credited artists already present
    in the bucket (dedup), so the front can toast "N 담음 · M 중복 건너뜀"."""
    added: List[ArtistBrief] = Field(default_factory=list)
    skipped: List[ArtistBrief] = Field(default_factory=list)


class ArtistExpansionResponse(BaseModel):
    """FEAT-my-buckit-artist (V32): the add-item response for a source_* artist expansion (a
    featuring track / compilation album expanded into its credited artists). This is a DISTINCT
    shape from BucketItemResponse — there is no single membership row to echo, only the
    added/skipped lists — so BucketItemResponse stays strict (id/position/status required) and
    POST /items returns a Union of the two. The front discriminates on the presence of
    `expansion` (only this shape carries it)."""
    item_type: Literal["artist"] = "artist"
    expansion: BucketItemExpansion


class BucketItemResponse(BaseModel):
    id: str
    # FEAT-pocket-buckit Step 3: typed membership discriminator (defaults 'album'). The
    # serializer is now non-album-TOLERANT — album_id/album are Optional and null on a
    # non-album row (track/review/playback/snapshot). This read-side change ships and is
    # prod-verified BEFORE the STEP-2 relax that lets such rows exist, so the first
    # non-album row can never 500 GET /api/buckets (the carried serializer-before-relax
    # gate). track_id/review_target_id echo the typed FK when set.
    item_type: str = "album"
    album_id: Optional[str] = None
    track_id: Optional[str] = None
    review_target_id: Optional[str] = None
    # FEAT-my-buckit-artist (V32): the artist FK for an artist-kind row (else null).
    artist_id: Optional[str] = None
    position: int
    note: Optional[str] = None
    status: str
    post_id: Optional[str] = None
    rec_reason: Optional[str] = None
    # Advisory badge: album already has a published review (in post_albums).
    already_reviewed: bool = False
    # FEAT-album-research-notes: per-item auto-research checkbox (see UpdateBucketItemRequest).
    research_selected: bool = False
    # FEAT-album-research-notes: latest research-note status for this album
    # ('queued'|'running'|'done'|'failed'), or null when never researched. Lets the
    # cover badge render the done/in-progress dot on first paint without a per-cover
    # GET (the note GET stays the on-open / live-poll source of truth).
    research_status: Optional[str] = None
    # FEAT-editor-buckit Stage 1: "오늘 밤 키우기" gate (see UpdateBucketItemRequest).
    prep_tonight: bool = False
    # Optional (FEAT-pocket-buckit Step 3): present on album rows, null on non-album rows.
    album: Optional[AlbumBrief] = None
    # Optional (FEAT-pocket-buckit Step 6): present on track/playback rows, null otherwise.
    track: Optional[TrackBrief] = None
    # FEAT-my-buckit-artist (V32): present on artist rows, null otherwise. (A source_* artist
    # EXPANSION returns the separate ArtistExpansionResponse, not this model.)
    artist: Optional[ArtistBrief] = None


class BucketResponse(BaseModel):
    id: str
    name: str
    position: int
    color: Optional[str] = None
    is_done: bool
    # FEAT-spotify-library-sync: 'review' (normal kanban column) or 'spotify_library'
    # (the single bucket mirroring the owner's Spotify saved-albums Library). Lets the
    # FRONT identify/filter the special bucket out of the normal tree.
    kind: str = "review"
    # FEAT-my-buckit-artist (V32): bucket-level type discriminator — 'general' (today's
    # polymorphic membership) | 'artist' (artist members only). Orthogonal to `kind`
    # (role/system). Every existing & system bucket is 'general'. The front renders the
    # General/Artist segmented control + type-filter chips off this.
    type: str = "general"
    # FEAT-album-research-notes: auto-research scope for this bucket
    # ('off' | 'all' | 'selected'). The front renders the off/전체/선택 control.
    research_mode: str = "off"
    # FEAT-public-bucket-multiuser Scope A: whether this bucket is published to the
    # public read-only viewer. Owner-only view (GET /api/buckets is now Cognito-gated).
    is_public: bool = False
    items: List[BucketItemResponse] = Field(default_factory=list)
    # FEAT-member-dashboard Step 5: nested tree. A bucket's descendants are
    # inlined here (recursive). The top-level BucketsResponse.buckets list holds
    # only roots (parent_id IS NULL); every level is ordered (position, created_at).
    # No parent_id field — the tree is explicit via this children nesting.
    children: List["BucketResponse"] = Field(default_factory=list)


# Resolve the forward reference in the recursive `children` annotation.
BucketResponse.model_rebuild()


class BucketsResponse(BaseModel):
    buckets: List[BucketResponse] = Field(default_factory=list)


# ── Public bucket viewer (FEAT-public-bucket-multiuser Scope A) ──────────────────
# A deliberately WHITELISTED projection served unauthenticated at
# GET /api/buckets/public. It exposes ONLY album-shelf information for is_public=true,
# kind='review' buckets. It intentionally OMITS every private field — bucket items'
# `note` / `rec_reason` / `status` / `post_id` / `research_*`, and the bucket's
# `is_done` / `kind` / `research_mode` / nesting. Never widen this without review.

class PublicAlbumBrief(BaseModel):
    id: str
    title: str
    cover_url: Optional[str] = None
    release_date: Optional[date] = None
    artist_names: List[str] = Field(default_factory=list)
    genres: List[str] = Field(default_factory=list)


class PublicBucketItem(BaseModel):
    album_id: str
    position: int
    # Public-safe: the album already has a published (public) review on this site.
    already_reviewed: bool = False
    album: PublicAlbumBrief


class PublicBucket(BaseModel):
    id: str
    name: str
    position: int
    color: Optional[str] = None
    items: List[PublicBucketItem] = Field(default_factory=list)


class PublicBucketsResponse(BaseModel):
    buckets: List[PublicBucket] = Field(default_factory=list)


# ====== Album research notes (FEAT-album-research-notes) ======

class ResearchTriggerRequest(BaseModel):
    model_config = {"extra": "ignore"}

    # No mode = first-time manual trigger (no-op if a row already exists).
    # 'restart' = full redo (clears the note); 'refine' = incremental update that
    # keeps the existing note and applies `instruction` (valid on a done row only).
    mode: Optional[Literal["restart", "refine"]] = None
    instruction: Optional[str] = None


class AlbumResearchResponse(BaseModel):
    album_id: str
    prompt_version: str
    status: str  # queued | running | done | failed
    model: Optional[str] = None
    result_md: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    search_count: Optional[int] = None
    error: Optional[str] = None
    refine_count: int = 0
    last_instruction: Optional[str] = None
    requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ResearchStatusMapResponse(BaseModel):
    """Batched album_id -> research status, for the cover dots. One query for a
    whole board instead of one GET per cover (the per-cover fan-out throttled the
    Lambda → 503s). Albums with no note are simply absent from the map."""
    statuses: Dict[str, str]


class MoveBucketRequest(BaseModel):
    # FEAT-member-dashboard Step 5: reparent + reposition a bucket.
    # parent_id null => move to root. position is the target slot among the new
    # parent's children (siblings renumbered 0..n contiguous after the move).
    parent_id: Optional[str] = None
    position: int


# ====== Spotify Library sync (FEAT-spotify-library-sync) ======
# The single kind='spotify_library' bucket mirrors the owner's Spotify saved-albums
# Library. POST .../sync only ENQUEUES an async job (rule #9 — never calls Spotify);
# the worker does the reads/diffs/writes. GET .../state reads the worker-written
# spotify_library_albums table for the /profile board badges + banners.

class SpotifyLibrarySyncResponse(BaseModel):
    # "queued" = a sync job was enqueued; "debounced" = a sync ran within the
    # server-side debounce window (~30s) so this call was a no-op.
    status: str


class SpotifyLibraryAlbumState(BaseModel):
    album_id: str
    spotify_id: str
    # source: "myblog_added" | "preexisting" (first-touch stamp by the worker).
    source: str
    # state: "pending" | "synced" | "failed" | "needs_attention".
    state: str
    # Bucket intent vs last-observed Spotify Library membership.
    in_bucket: bool
    in_spotify: bool
    last_error: Optional[str] = None


class SpotifyLibraryStateResponse(BaseModel):
    # Null when the special bucket doesn't exist yet (no sync ever run).
    bucket_id: Optional[str] = None
    # max(spotify_library_albums.last_synced_at) — the UI polls this after a manual
    # sync until it advances, then refetches (mirrors recently-listened's poll).
    last_synced_at: Optional[datetime] = None
    # From get_spotify_connection_status(): the worker's last refresh hit invalid_grant.
    needs_reauth: bool = False
    # Read-only mirror of the worker write gate (SPOTIFY_LIBRARY_WRITES_ENABLED), for
    # the "검토 모드" banner. False = plan-only; the worker issues no real Spotify writes.
    writes_enabled: bool = False
    albums: List[SpotifyLibraryAlbumState] = Field(default_factory=list)


# ====== Playback token (FEAT-pocket-buckit Step 3, D3 / OQ8) ======
# GET /api/playback/spotify-token async-mints a short-lived Spotify Web Playback SDK
# access token from the owner's per-listener `streaming`-scope refresh token (rule #9:
# the play path is client-side; the server only mints/refreshes a token, never proxies a
# Spotify content call). Dormant until the Step-5 `streaming` OAuth consent provisions the
# refresh token — until then the route is a real Cognito-JWT route that returns 503.

class SpotifyStreamingTokenResponse(BaseModel):
    # The short-lived Web Playback SDK access token + its lifetime (Spotify returns ~3600s).
    # The front re-mints before expiry; the token itself is never persisted server-side.
    access_token: str
    expires_in: int
    token_type: str = "Bearer"


class PlaybackResolveResponse(BaseModel):
    # A Spotify URI resolved from a catalog DB id via the stored spotify_id (FEAT-spotify-
    # streaming-playback Step 2). spotify:track:<id> is played as a uris[] entry;
    # spotify:album:<id> as a context_uri (full-album context).
    uri: str


# ====== Member library (FEAT-member-dashboard Step 2, D18) ======
# Two sources: 들을 것 (to-listen queue, a real table) and 평론한 앨범 (reviewed,
# a derived view over published posts). 최근 들은 앨범 is Step 3.

class AddToListenRequest(BaseModel):
    album_id: str = Field(min_length=1)
    note: Optional[str] = None


class ToListenReorderRequest(BaseModel):
    # Top→bottom item order for the single queue (same idempotent mechanism as
    # /api/buckets/reorder; one list since to-listen has no columns).
    item_ids: List[str] = Field(default_factory=list)


class ToListenItemResponse(BaseModel):
    id: str
    album_id: str
    position: int
    note: Optional[str] = None
    added_at: datetime
    album: AlbumBrief


class ToListenResponse(BaseModel):
    items: List[ToListenItemResponse] = Field(default_factory=list)


class ReviewedAlbumResponse(BaseModel):
    # Card unit = album (D20); review_ids are the album's published posts (M:N).
    album_id: str
    review_ids: List[str] = Field(default_factory=list)
    album: AlbumBrief


class ReviewedResponse(BaseModel):
    items: List[ReviewedAlbumResponse] = Field(default_factory=list)


# ====== Member listening (FEAT-member-dashboard Step 3, D25/D26/D5) ======
# 최근 들은 앨범 + now-playing, read from a worker/EventBridge-fed Spotify cache
# (spotify_recent_albums / spotify_now_playing). No synchronous Spotify call from
# these endpoints (hard rule #9).

class RecentlyListenedItem(BaseModel):
    album_id: str
    last_played_at: datetime
    album: AlbumBrief


class RecentlyListenedResponse(BaseModel):
    items: List[RecentlyListenedItem] = Field(default_factory=list)
    # When the worker last wrote this cache (max synced_at), or None when empty.
    # The /profile UI polls this after a manual refresh until it advances (D31),
    # instead of guessing with a fixed delay.
    last_synced_at: Optional[datetime] = None


class NowPlayingResponse(BaseModel):
    is_playing: bool = False
    track: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_id: Optional[str] = None
    # Catalog cover for the playing album (Album.cover_url via the album_id FK),
    # so the now-playing UI can show real art instead of a letter tile. None when
    # the album isn't in our catalog or nothing is playing.
    album_cover_url: Optional[str] = None
    # progress_ms/duration_ms intentionally omitted (D28): now-playing is an
    # intentionally-public single-admin vanity read, so we don't expose
    # fine-grained real-time position; a <=1h-stale snapshot also can't advance
    # a progress bar, so it'd only ever render a misleading frozen one.
    updated_at: Optional[datetime] = None


class SpotifyConnectionResponse(BaseModel):
    # Status for the /profile → 연동 tab. Reflects token *validity*, not mere presence
    # (D30): connected = a refresh token is stored; needs_reauth = the worker's last
    # refresh hit invalid_grant (token revoked/expired → "재인증 필요");
    # last_successful_refresh_at = when the token last worked. (in-app OAuth still
    # deferred per D27; token is bootstrapped out-of-band.)
    connected: bool = False
    needs_reauth: bool = False
    last_successful_refresh_at: Optional[datetime] = None


class RefreshRecentResponse(BaseModel):
    status: str  # "queued"


# ====== Member listening — durable data (FEAT-member-dashboard-realdata) ======
# Goal 5 keeps these distinct from the cache above:
#   - 최근 재생 트랙  → spotify_recent_tracks (rolling track-level CACHE, D-B). Its
#     head doubles as the "최근 재생" (latest-played) now-playing fallback (D-C).
#   - 들은 앨범(누적) → aggregate of spotify_play_events (append-only log), NOT a
#     table (D-A): per-album play_count + first/last play, the durable archive.

class RecentTrackItem(BaseModel):
    spotify_track_id: str
    track_name: str
    artist_name: Optional[str] = None
    album_name: Optional[str] = None
    # Resolved catalog album id + brief when the track's album is in our catalog;
    # null/None for tracks whose album we don't have (still shown, denormalized).
    album_id: Optional[str] = None
    album: Optional[AlbumBrief] = None
    played_at: datetime


class RecentTracksResponse(BaseModel):
    items: List[RecentTrackItem] = Field(default_factory=list)
    # When the worker last wrote the recent-tracks cache (max synced_at), or None.
    last_synced_at: Optional[datetime] = None


class ListenedAlbumItem(BaseModel):
    album_id: str
    play_count: int
    first_played_at: datetime
    last_played_at: datetime
    album: AlbumBrief


class ListenedAlbumsResponse(BaseModel):
    items: List[ListenedAlbumItem] = Field(default_factory=list)
    # Total distinct listened albums (for pagination / a "N albums" stat).
    total: int = 0


# ====== 분석 버킷: saved tracks + genre/artist distribution (FEAT-genre-artist-distribution) ======
# One shared DistributionResponse shape for BOTH sources (좋아요 / 재생) so the front
# chart component is source-agnostic. items is a clean partition:
#   sum(item.count) + unclassified_count == total.

class SavedTrackItem(BaseModel):
    spotify_track_id: str
    track_name: str
    artist_name: Optional[str] = None
    album_name: Optional[str] = None
    album_sid: Optional[str] = None
    # Resolved catalog album id + brief when the track's album is in our catalog;
    # None for saved tracks whose album we don't have (still shown, denormalized).
    album_id: Optional[str] = None
    album: Optional[AlbumBrief] = None
    added_at: datetime
    # FEAT-liked-tracks-workbench Step 4: track length (ms) from /me/tracks.
    # None for rows synced before the worker started writing it (backfills on
    # the next weekly mode=full sync).
    duration_ms: Optional[int] = None


class SavedTracksResponse(BaseModel):
    items: List[SavedTrackItem] = Field(default_factory=list)
    total: int = 0
    last_synced_at: Optional[datetime] = None


class DistItem(BaseModel):
    label: str
    count: int


class UnclassifiedBreakdown(BaseModel):
    # Genre charts only (artist charts leave it null). uncatalogued = album not in our
    # catalog (album_id NULL); ungenred = catalog-present but no album_genres (needs
    # the iTunes backfill). These sum to the genre chart's unclassified_count.
    uncatalogued: int = 0
    ungenred: int = 0


class DistributionResponse(BaseModel):
    items: List[DistItem] = Field(default_factory=list)
    # 미분류: items with no resolved genre / no artist. Shown on the chart as a chip,
    # with "전체 N곡 중" as the stated denominator (total).
    unclassified_count: int = 0
    total: int = 0
    # Genre charts carry the uncatalogued/ungenred breakdown; artist charts null.
    unclassified_breakdown: Optional[UnclassifiedBreakdown] = None
    last_synced_at: Optional[datetime] = None


# ====== 분석 버킷: lifetime stream history (FEAT-listening-history-import) ======
# Ungated count/time top-N over spotify_stream_history (the GDPR Extended Streaming
# History import). Coverage-INDEPENDENT: aggregates the denormalized track/artist text
# + the stable spotify_track_uri, no catalog FK — so it ships regardless of the album
# resolution rate (the album/era/genre panels are the gated ones). `value` carries
# plays when unit="count" and ms_played when unit="ms" (the front Count↔Time toggle);
# `unit` names the axis so one chart component renders both.

class StreamRankItem(BaseModel):
    label: str                                  # track name (top-tracks) | artist name (top-artists)
    artist: Optional[str] = None                # the track's (album) artist — top-tracks only
    spotify_track_uri: Optional[str] = None     # drill-down handle — top-tracks only
    value: int                                  # plays (unit="count") or ms_played (unit="ms")


class StreamRankResponse(BaseModel):
    items: List[StreamRankItem] = Field(default_factory=list)
    unit: str = "count"                         # "count" | "ms" — the Count↔Time axis
    # 미분류 weight (plays or ms) for the coverage-GATED genre/era panels: streams whose
    # album is uncatalogued OR has no genre / no release_date. 0 for the ungated
    # track/artist rankings (Step 4). Shown as the chart's 미분류 bar.
    unclassified: int = 0
    # Honesty captions (mirrors #194): the population denominator + the import horizon.
    total_streams: int = 0                      # music streams in scope (≥30s, not skipped, not podcast)
    total_ms: int = 0                           # total listening time (ms) in the same scope
    as_of: Optional[datetime] = None            # max import ts — the lifetime/live boundary + staleness horizon
    # FEAT-listening-live-merge: plays folded in from the live recently-played tail
    # (event after as_of). >0 ⇒ the time figure includes an ESTIMATE (poller has no ms).
    live_streams: int = 0


# ── Step 5 (GATED on the Step-3 coverage rate): album / era / genre / retrospective ──
# All need the catalog FK (+ Album.release_date for era). Gate PASSED at 99.7% album /
# 99.96% release_date (Step 3), so these ship; the `unresolved`/`unclassified` weights
# carry the residual 미분류 for the honesty caption.

class StreamAlbumRankItem(BaseModel):
    album: AlbumBrief
    value: int                                  # plays (unit="count") or ms_played (unit="ms")


class StreamAlbumRankResponse(BaseModel):
    items: List[StreamAlbumRankItem] = Field(default_factory=list)
    unit: str = "count"                         # "count" | "ms"
    unresolved: int = 0                         # in-scope streams (weight) with no catalog album — the gate caption
    total_streams: int = 0
    total_ms: int = 0
    as_of: Optional[datetime] = None
    live_streams: int = 0                        # plays from the live tail (estimated time) — FEAT-listening-live-merge


class RetroYearStat(BaseModel):
    year: int                                   # KST calendar year (ts AT TIME ZONE 'Asia/Seoul')
    plays: int
    ms_played: int


class OnThisDayItem(BaseModel):
    year: int                                   # the KST year this stream fell on today's month/day
    track_name: Optional[str] = None
    artist_name: Optional[str] = None
    album_id: Optional[str] = None
    album: Optional[AlbumBrief] = None          # populated when the track's album is catalogued
    plays: int
    ms_played: int


class RetrospectiveResponse(BaseModel):
    per_year: List[RetroYearStat] = Field(default_factory=list)
    # "on this day" across past years (today's month+day in KST, server now()).
    on_this_day: List[OnThisDayItem] = Field(default_factory=list)
    today_kst: str                              # "MM-DD" the server bucketed on (front caption)
    as_of: Optional[datetime] = None
    live_streams: int = 0                        # plays from the live tail (estimated time) — FEAT-listening-live-merge


# ── FEAT-analysis-explore: item drill-down + listening clock ────────────────────────
# Re-aggregations of the same lifetime+live union under an optional [from, to) range
# (raw timestamps; the front maps presets → from/to). The drill-down keys one entity
# (artist | catalog album | track); the clock is a KST hour×weekday matrix. Honesty
# fields (as_of, live_streams) carry through — count exact, live-tail time estimated.

class StreamItemDetailResponse(BaseModel):
    type: str                                   # "artist" | "album" | "track" (echo)
    id: str                                     # the entity key (artist_name | album_id | track uri) — echo
    label: str                                  # display name (artist name / album title / track name)
    artist: Optional[str] = None                # the entity's artist line (album/track); null for an artist entity
    unit: str = "count"                         # "count" | "ms" — names the top_tracks/top_albums `value` axis
    count: int = 0                              # plays in range
    time_ms: int = 0                            # listening time (ms) in range
    first_listen: Optional[datetime] = None     # earliest event_ts in range (null when no plays)
    last_listen: Optional[datetime] = None      # latest event_ts in range
    per_year: List[RetroYearStat] = Field(default_factory=list)   # KST per-year mini-history
    # An artist entity also carries its own top tracks/albums (same shape as the panels);
    # album/track entities leave these empty.
    top_tracks: List[StreamRankItem] = Field(default_factory=list)
    top_albums: List[StreamAlbumRankItem] = Field(default_factory=list)
    as_of: Optional[datetime] = None            # global import horizon (staleness marker)
    live_streams: int = 0                        # this entity's live-tail plays (>0 ⇒ time is an estimate)


class StreamClockCell(BaseModel):
    weekday: int                                # Postgres extract(dow): 0=Sun … 6=Sat (Asia/Seoul)
    hour: int                                   # 0..23 (Asia/Seoul)
    plays: int
    ms_played: int


class StreamClockResponse(BaseModel):
    # Only the non-empty cells (≤168); the front fills the 7×24 grid with zeros.
    cells: List[StreamClockCell] = Field(default_factory=list)
    unit: str = "count"                         # "count" | "ms" — which weight the front colours by
    total_streams: int = 0
    total_ms: int = 0
    as_of: Optional[datetime] = None
    live_streams: int = 0


class ClassifyResponse(BaseModel):
    # 분류하기: catalog-absent unclassified albums enqueued for catalog sync (→ S1
    # genres). Catalog-present-but-ungenred tracks can't be fixed via SQS (they need
    # the iTunes backfill), surfaced as skipped_needs_backfill rather than enqueued.
    enqueued: int = 0
    skipped_needs_backfill: int = 0
    status: str = "queued"


class FillGenresResponse(BaseModel):
    # 장르 채우기: enqueue an on-demand genre-backfill run (the local poller runs the
    # existing S1→S2→S3 pipeline). "already_pending" when a request is already waiting
    # for the poller (deduped to one pending request).
    status: str = "queued"  # "queued" | "already_pending"


# ====== Sections (STAB-5) ======
# Read-only seeded taxonomy. The post request/response bodies keep the JSON
# field name `category` for now (contract rename deferred to Step 5); only the
# DB axis + this list endpoint moved to `section`.

class SectionItem(BaseModel):
    name: str
    slug: str


class SectionListResponse(BaseModel):
    sections: List[SectionItem]


# ====== Tags (STAB-5 Step 4) ======
# Read-only seeded review-tag vocabulary. Cross-cutting M:N over `post_tags`,
# distinct from the single-FK section. No create endpoint (mirrors sections).

class TagItem(BaseModel):
    name: str
    slug: str


class TagListResponse(BaseModel):
    tags: List[TagItem]


# ====== Metrics ======

class MetricsBatchRequest(BaseModel):
    slugs: List[str]


class PostMetrics(BaseModel):
    likes: int
    comments: int


class MetricsBatchResponse(BaseModel):
    data: Dict[str, PostMetrics]


# ====== Genres (FEAT-genre-system Step 4) ======
# Machine-labeled tier-0 taxonomy (12 fixed nodes) + owner-editable definitions.
# parent_id ships now so the tier-1 sub-genre RFC attaches without migration.
# GET /api/genres/tree is public (edge_guard catch-all); POST/PUT are Cognito-JWT.

class GenreNode(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    slug: str
    label: str
    parent_id: Optional[str] = None
    definition_md: str = ""
    position: int = 0
    # Albums tagged with this genre in `album_genres` (all confidences). Drives the
    # /genres map's share-bar distribution (e.g. Hip-Hop ~49%). Read-only, derived —
    # NOT the human-editable surface. Direct attachments only (children roll up
    # their own; tier-0 genres are attached directly by the machine pipeline).
    album_count: int = 0
    # Nested children (tier-1 under tier-0). Empty for leaf/tier-1 nodes today.
    children: List["GenreNode"] = Field(default_factory=list)


class GenreEdge(BaseModel):
    """A tier-1 relationship edge (FEAT-genre-subgenres). Drives the /genres
    ego-view + the related-review recommendation. `type` ∈ influenced_by | related
    | parent (the multi-parent fallback, unused in v1)."""
    from_id: str
    to_id: str
    type: str


class GenreTreeResponse(BaseModel):
    genres: List[GenreNode]
    # FEAT-genre-subgenres: the relationship graph (read-only here; owner authoring
    # is Step 6). Empty until edges are seeded/added.
    edges: List[GenreEdge] = Field(default_factory=list)


class CreateGenreRequest(BaseModel):
    slug: str = Field(min_length=1)
    label: str = Field(min_length=1)
    parent_id: Optional[str] = None
    definition_md: str = ""
    position: Optional[int] = None


class UpdateGenreRequest(BaseModel):
    model_config = {"extra": "ignore"}

    # All optional; exclude_unset on the route distinguishes "not provided" from an
    # explicit clear (definition_md=""). slug/parent_id are not mutable here — the
    # taxonomy shape is owner-stable; only label/definition/order are edited.
    label: Optional[str] = None
    definition_md: Optional[str] = None
    position: Optional[int] = None


# ====== Lyrics (FEAT-lyrics-viewer Step 1) ======
# Normalized payload spec pinned in ARCH-lyrics-normalization-model (2026-07-02).
# JWT-gated /api/lyrics read ONLY — never referenced by any public/edge-cached response
# (track_lyrics privacy bar: "never in any shared response").

class LyricsSegment(BaseModel):
    # Index-keyed (not id-keyed) so a future split/merge can rewrite the list without
    # breaking the viewer; text=="" is the stanza-gap discriminator (no extra field);
    # start_ms is optional-from-day-one so estimated timing lands as an additive upgrade.
    i: int
    text: str
    start_ms: Optional[int] = None
    # Korean line (FEAT-lyrics-translation Step 2): populated ONLY when the track's
    # translation is done AND its source_fingerprint matches the current normalization;
    # a mismatch reads as translation.status == "stale" with text_ko omitted.
    text_ko: Optional[str] = None


class LyricsTranslationInfo(BaseModel):
    # "none" = no request row yet; "stale" is computed at read time (stored fingerprint
    # != current normalization), never stored — DB rows only hold requested|done|failed.
    status: Literal["none", "requested", "done", "failed", "stale"]
    origin: Optional[Literal["poller", "manual"]] = None
    lang: Optional[str] = None


class LyricsResponse(BaseModel):
    availability: Literal["ok", "no_lyrics", "unavailable"]
    # Present iff availability == "ok".
    source_kind: Optional[Literal["synced", "plain"]] = None
    # True iff ≥1 non-gap segment carries start_ms — drives the viewer's optional
    # one-shot initial focus; false → first-segment focus.
    trackable: bool = False
    # Cache-invalidation hook (mirrors matcher_version).
    normalizer_version: int = 1
    # [] unless availability == "ok"; file order preserved.
    segments: List[LyricsSegment] = Field(default_factory=list)
    # Present iff availability == "ok" (additive, FEAT-lyrics-translation Step 2).
    translation: Optional[LyricsTranslationInfo] = None


# ====== Me (FEAT-multi-user-accounts Phase 0 / 0d) ======

class MeResponse(BaseModel):
    id: str
    email: Optional[str] = None
    handle: str
    display_name: str
    avatar_url: Optional[str] = None
    created_at: datetime


# ── FEAT-multi-user Phase 3a — listening/AI integrations (Last.fm this step) ──
class IntegrationResponse(BaseModel):
    provider: str
    username: Optional[str] = None
    status: str
    last_synced_at: Optional[datetime] = None


class IntegrationsResponse(BaseModel):
    integrations: list[IntegrationResponse]


class ConnectLastfmRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class LastfmNowPlayingResponse(BaseModel):
    is_playing: bool
    artist: Optional[str] = None
    track: Optional[str] = None
    album: Optional[str] = None
    image_url: Optional[str] = None
    played_at: Optional[datetime] = None


class UpdateMeRequest(BaseModel):
    model_config = {"extra": "ignore"}

    # Mirrors ck_users_handle_format (V36) so a bad handle 422s at the edge
    # instead of surfacing as a CHECK-violation 500. None = field not updated
    # (neither column is nullable, so an explicit null is treated as omitted).
    handle: Optional[str] = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$"
    )
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)


# ====== Album reviews (FEAT-multi-user-accounts Phase 1) ======
# RYM-style public reviews: one rating (+ optional comment) per member per album.
# Aggregates (avg/count) are computed live at read time (OQ2). All reads public.

# Comment length ceiling — a lightweight review, not an essay (the owner's
# long-form editorial stays on the GitHub-MDX path).
REVIEW_COMMENT_MAX = 4000


class AlbumReviewUpsertRequest(BaseModel):
    model_config = {"extra": "ignore"}

    # Mirrors ck_album_reviews_rating_halfstep (V38): 0.5–5.0 in half-steps, so a
    # bad rating 422s at the edge instead of surfacing as a CHECK-violation 500.
    rating: float = Field(..., ge=0.5, le=5.0, multiple_of=0.5)
    comment: Optional[str] = Field(default=None, max_length=REVIEW_COMMENT_MAX)


class ReviewAuthor(BaseModel):
    """The public reviewer identity embedded in an album's review list."""
    id: str
    handle: str
    display_name: str
    avatar_url: Optional[str] = None


class AlbumReviewResponse(BaseModel):
    id: str
    album_id: str
    author: ReviewAuthor
    rating: float
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AlbumReviewAggregateResponse(BaseModel):
    """Album-page block: live avg/count + the full public review list."""
    album_id: str
    average: Optional[float] = None  # None when count == 0
    count: int
    reviews: List[AlbumReviewResponse] = Field(default_factory=list)


class MemberReviewResponse(BaseModel):
    """One row in a member's public profile feed — the review plus enough album
    context to render + link without a second fetch."""
    id: str
    album_id: str
    album_title: str
    album_cover_url: Optional[str] = None
    rating: float
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MemberProfileResponse(BaseModel):
    """Public profile at /members/{handle}: identity + newest-first review feed."""
    handle: str
    display_name: str
    avatar_url: Optional[str] = None
    created_at: datetime
    review_count: int
    reviews: List[MemberReviewResponse] = Field(default_factory=list)


class MemberSummary(BaseModel):
    handle: str
    display_name: str
    avatar_url: Optional[str] = None
    review_count: int


class MemberListResponse(BaseModel):
    """Handle index for the front's getStaticPaths (static profile prerender)."""
    members: List[MemberSummary] = Field(default_factory=list)