# app/api/schemas.py
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from datetime import date, datetime


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
    recommended_track_ids: List[str] = Field(default_factory=list)
    # FEAT-writer-lowfreq-redesign Step 5: joined from the post's subject album
    # so the writer's edit flow can seed the BEST NEW pill on load. Null when
    # the post has zero or many albums (no single subject to read from).
    subject_best_new: Optional[bool] = None


# ====== Review buckets (FEAT-review-bucket-board) ======

class CreateBucketRequest(BaseModel):
    name: str = Field(min_length=1)
    color: Optional[str] = None


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


class AddBucketItemRequest(BaseModel):
    album_id: str = Field(min_length=1)
    note: Optional[str] = None


class UpdateBucketItemRequest(BaseModel):
    model_config = {"extra": "ignore"}

    note: Optional[str] = None
    status: Optional[Literal["candidate", "drafting", "published"]] = None
    post_id: Optional[str] = None
    # FEAT-album-research-notes: per-item checkbox, meaningful only while the parent
    # bucket's research_mode='selected'. Checking it (in 'selected' mode) enqueues.
    research_selected: Optional[bool] = None


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


class BucketItemResponse(BaseModel):
    id: str
    album_id: str
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
    album: AlbumBrief


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
    # FEAT-album-research-notes: auto-research scope for this bucket
    # ('off' | 'all' | 'selected'). The front renders the off/전체/선택 control.
    research_mode: str = "off"
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
    # Nested children (tier-1 under tier-0). Empty for leaf/tier-1 nodes today.
    children: List["GenreNode"] = Field(default_factory=list)


class GenreTreeResponse(BaseModel):
    genres: List[GenreNode]


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