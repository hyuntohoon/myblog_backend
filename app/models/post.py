# app/models/post.py
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Literal, List
import re
import uuid

def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "untitled"

# 1) 쓰기용: 폼/요청 → 유효성/슬러그 생성까지
@dataclass
class PostDraft:
    title: str
    body_mdx: str
    description: str = ""
    posted_date: date = field(default_factory=lambda: date.today())
    status: Literal["draft", "published", "archived"] = "published"

    # 카테고리는 이름 기준(서비스에서 id로 resolve)
    category_name: Optional[str] = None

    # (옵션) 음악 리뷰 작성시 함께 들어오는 초안 정보
    music_review_subject: Optional[Literal["album", "track"]] = None
    review_target_id: Optional[str] = None
    rating: Optional[float] = None

    def validate(self) -> None:
        if not self.title:
            raise ValueError("title required")
        if not self.body_mdx:
            raise ValueError("body_mdx required")
        if self.rating is not None and not (0 <= self.rating <= 10):
            raise ValueError("rating must be 0~10")

    def to_persistence_payload(self, *, category_id: Optional[int]) -> dict:
        """DB INSERT에 바로 쓰는 payload 생성 (posts 테이블 컬럼 기준)."""
        self.validate()
        return {
            "id": str(uuid.uuid4()),
            "slug": slugify(self.title),
            "title": self.title.strip(),
            "description": self.description or "",
            "body_mdx": self.body_mdx,
            "posted_date": self.posted_date,
            "status": self.status,
            "category_id": category_id,
            # DB가 채우는 필드: last_updated_at, body_text, search_index, extra 등은 생략
        }

# 2) 읽기용: DB 레코드 1:1 매핑
@dataclass
class Post:
    id: str
    slug: str
    title: str
    description: str
    body_mdx: str
    posted_date: date
    last_updated_at: datetime
    status: str
    category_id: Optional[int]