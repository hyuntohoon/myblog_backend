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
    rating_scale: int = Field(default=5, ge=1, le=10)

    # 앨범별 명반 여부
    album_classics: Dict[str, bool] = Field(default_factory=dict)
    # 예: {"album-uuid-1": true, "album-uuid-2": false}

    # 추천 트랙
    recommended_tracks: List[RecommendedTrackInput] = Field(default_factory=list)


class WritePostResponse(BaseModel):
    id: str
    slug: str


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