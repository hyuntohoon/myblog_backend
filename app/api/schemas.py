# app/api/schemas.py
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from datetime import date

# ====== Posts ======

class WritePostRequest(BaseModel):
    """
    프런트 입력을 서버 내부 모델로 정리:
    - body -> body_mdx 로 alias
    - posted_date 기본값: 오늘
    - status: 'draft' | 'published' | 'archived'
    - category: 카테고리 '이름' (서비스에서 id resolve)
    - searchIndex(optional) -> search_index 로 alias (DB 기본 True)
    - 음악 리뷰 필드(옵션): subject/target/rating
    """
    model_config = {
        "populate_by_name": True,   # alias 로 들어온 값을 수용
        "extra": "ignore",          # 정의 안 된 키는 무시(테스트 편의)
    }

    title: str = Field(min_length=1)
    body_mdx: str = Field(alias="body", min_length=1)
    description: str = ""
    posted_date: date = Field(default_factory=date.today)
    status: Literal["draft", "published", "archived"] = "published"

    # 카테고리는 '이름'으로 받음 (서비스에서 id 탐색/생성)
    category: Optional[str] = None

    # DB의 search_index (요청에 없으면 기본 True 사용)
    search_index: Optional[bool] = Field(default=True, alias="searchIndex")

    # --- Music Review (옵션) ---
    music_review_subject: Optional[Literal["album", "track"]] = Field(
        default=None, alias="musicReviewSubject"
    )
    review_target_id: Optional[str] = Field(default=None, alias="reviewTargetId")
    rating: Optional[float] = Field(default=None, ge=0, le=10)

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