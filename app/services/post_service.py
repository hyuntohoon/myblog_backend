# app/services/post_service.py
from __future__ import annotations

from datetime import date
from typing import Optional
from sqlalchemy.orm import Session

from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from app.models.post import Post
import re


def slugify_title(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "untitled"


class PostService:
    """
    - 카테고리: 이름이 오면 get_or_create로 upsert (없으면 생성)
    - 슬러그: 중복 시 '-2', '-3' … 자동 suffix 부여로 유니크 보장
    """
    def __init__(self, post_repo: PostRepository, category_repo: CategoryRepository):
        self.post_repo = post_repo
        self.category_repo = category_repo

    def create(
            self,
            db: Session,
            *,
            title: str,
            description: str = "",
            body_mdx: str,
            posted_date: date,
            status: str = "published",
            category_name: Optional[str] = None,
    ) -> Post:
        # 1) 카테고리 upsert → id
        category_id: Optional[int] = None
        if (category_name or "").strip():
            cat = self.category_repo.get_by_name(db, category_name.strip())
            if not cat:
                cat = self.category_repo.create(db, category_name.strip())
            category_id = cat.id

        # 2) 슬러그 유니크 보장
        base_slug = slugify_title(title)
        slug = self._ensure_unique_slug(db, base_slug)

        # 3) 저장
        return self.post_repo.create(
            db,
            slug=slug,
            title=title.strip(),
            description=description or "",
            body_mdx=body_mdx,
            posted_date=posted_date,
            status=status,
            category_id=category_id,
        )

    # ---------------------------
    # Internal Helpers
    # ---------------------------
    def _ensure_unique_slug(self, db: Session, base: str) -> str:
        """
        base가 이미 있으면 base-2, base-3 … 순차적으로 찾아 유니크 보장
        """
        if not self.post_repo.get_by_slug(db, base):
            return base

        i = 2
        while True:
            candidate = f"{base}-{i}"
            exists = self.post_repo.get_by_slug(db, candidate)
            if not exists:
                return candidate
            i += 1