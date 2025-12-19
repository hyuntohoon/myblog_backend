# app/repositories/post_repository.py
from typing import List, Optional
from datetime import date

from sqlalchemy.orm import Session

from app.models.post import Post


class PostRepository:
    """posts 테이블 전용 리포지토리 (SQLAlchemy ORM 기반)."""

    def list_all(self, db: Session) -> List[Post]:
        """
        전체 포스트 목록을 posted_date DESC 로 반환
        """
        return (
            db.query(Post)
            .order_by(Post.posted_date.desc())
            .all()
        )

    def get_by_slug(self, db: Session, slug: str) -> Optional[Post]:
        """
        slug로 단일 포스트 조회
        """
        return (
            db.query(Post)
            .filter(Post.slug == slug)
            .first()
        )

    def create(
        self,
        db: Session,
        *,
        slug: str,
        title: str,
        description: str,
        body_mdx: Optional[str],  # NULL 허용
        posted_date: date,
        status: str,
        category_id: int,
        album_cover_url: Optional[str] = None,
        rating: Optional[float] = None,
        rating_scale: int = 5,           # 추가
        search_index: bool = True,       # 추가
    ) -> Post:
        post = Post(
            slug=slug,
            title=title,
            description=description,
            body_mdx=body_mdx,
            posted_date=posted_date,
            status=status,
            category_id=category_id,
            album_cover_url=album_cover_url,
            rating=rating,
            rating_scale=rating_scale,
            search_index=search_index,
        )

        db.add(post)
        db.flush()  # commit 대신 flush (ID 생성만)

        return post