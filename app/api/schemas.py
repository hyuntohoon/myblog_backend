# app/api/schemas.py
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from datetime import date


# ====== 추천 트랙 입력 ======

class RecommendedTrackInput(BaseModel):
    album_id: str
    track_id: str
    position: Optional[int] = None
    note: Optional[str] = None


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

    # 추천 트랙
    recommended_tracks: List[RecommendedTrackInput] = Field(default_factory=list)


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


# ====== Reviews ======
# Per-track ratings live in post_reviews (subject='track'). Album rating still
# lives on posts.rating (legacy) — see RFC PR-reviews-polymorphic.

class PostReviewAlbum(BaseModel):
    rating: float
    scale: int = 5


class PostReviewTrack(BaseModel):
    track_id: str
    rating: float
    scale: int = 5
    notes: Optional[str] = None


class PostReviewBundle(BaseModel):
    album: Optional[PostReviewAlbum] = None
    tracks: List[PostReviewTrack] = Field(default_factory=list)


class TrackReviewUpsert(BaseModel):
    rating: float = Field(ge=0, le=5)
    scale: int = 5
    notes: Optional[str] = None


class TrackReviewBatchItem(BaseModel):
    track_id: str
    rating: float = Field(ge=0, le=5)
    scale: int = 5
    notes: Optional[str] = None


class TrackReviewBatchRequest(BaseModel):
    tracks: List[TrackReviewBatchItem] = Field(default_factory=list, max_length=200)


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