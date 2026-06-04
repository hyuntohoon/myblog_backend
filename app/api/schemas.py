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


class AddBucketItemRequest(BaseModel):
    album_id: str = Field(min_length=1)
    note: Optional[str] = None


class UpdateBucketItemRequest(BaseModel):
    model_config = {"extra": "ignore"}

    note: Optional[str] = None
    status: Optional[Literal["candidate", "drafting", "published"]] = None
    post_id: Optional[str] = None


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
    album: AlbumBrief


class BucketResponse(BaseModel):
    id: str
    name: str
    position: int
    color: Optional[str] = None
    is_done: bool
    items: List[BucketItemResponse] = Field(default_factory=list)


class BucketsResponse(BaseModel):
    buckets: List[BucketResponse] = Field(default_factory=list)


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


# ====== Categories ======

class CategoryListResponse(BaseModel):
    categories: List[str]


class AddCategoryRequest(BaseModel):
    name: str = Field(min_length=1)


# ====== Metrics ======

class MetricsBatchRequest(BaseModel):
    slugs: List[str]


class PostMetrics(BaseModel):
    likes: int
    comments: int


class MetricsBatchResponse(BaseModel):
    data: Dict[str, PostMetrics]