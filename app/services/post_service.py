# app/services/post_service.py
from __future__ import annotations
import re
from datetime import date
from typing import Optional
from sqlalchemy.orm import Session

from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from app.models.post import Post
from app.models.album import Album, post_albums


def slugify_title(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "untitled"


class PostService:
    def __init__(self, post_repo: PostRepository, category_repo: CategoryRepository):
        self.post_repo = post_repo
        self.category_repo = category_repo

    def create(
        self,
        db: Session,
        *,
        title: str,
        description: str,
        body_mdx: str,
        posted_date: date,
        status: str = "published",
        category_name: Optional[str] = None,
        album_ids: Optional[list[str]] = None,
    ) -> Post:

        # ---------------------------
        # 1) 카테고리 resolve
        # ---------------------------
        category_id = None
        if category_name and category_name.strip():
            cat = self.category_repo.get_by_name(db, category_name.strip())
            if not cat:
                cat = self.category_repo.create(db, category_name.strip())
            category_id = cat.id

        # ---------------------------
        # 2) 슬러그 생성 + 중복 체크
        # ---------------------------
        base = slugify_title(title)
        slug = self._ensure_unique_slug(db, base)

        # ---------------------------
        # 3) Post 생성
        # ---------------------------
        post = self.post_repo.create(
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
        # 4) 앨범 연결 (N:N)
        # ---------------------------
        album_ids = album_ids or []
        if album_ids:
            # 중복 제거
            unique_ids = list({aid for aid in album_ids if aid})

            if unique_ids:
                albums = (
                    db.query(Album)
                    .filter(Album.id.in_(unique_ids))
                    .all()
                )

                for al in albums:
                    post.albums.append(al)

                db.flush()   # relationship 안전하게 반영

        return post

    # ---------------------------
    # 내부 헬퍼: 슬러그 유니크 보장
    # ---------------------------
    def _ensure_unique_slug(self, db: Session, base: str) -> str:
        if not self.post_repo.get_by_slug(db, base):
            return base

        i = 2
        while True:
            cand = f"{base}-{i}"
            if not self.post_repo.get_by_slug(db, cand):
                return cand
            i += 1