# app/repositories/post_repository.py
from typing import List, Optional
from datetime import date

from sqlalchemy.orm import Session

from myblog_shared_db.models import Post


class PostRepository:
    """posts 테이블 전용 리포지토리 (SQLAlchemy ORM 기반)."""

    def list_by_status(self, db: Session, status: Optional[str] = None) -> List[Post]:
        q = db.query(Post).order_by(Post.posted_date.desc())
        if status:
            q = q.filter(Post.status == status)
        return q.all()

    def list_all(self, db: Session) -> List[Post]:
        return self.list_by_status(db)

    def get_by_id(self, db: Session, post_id: str) -> Optional[Post]:
        return db.query(Post).filter(Post.id == post_id).first()

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
        section_id: Optional[int],
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
            section_id=section_id,
            album_cover_url=album_cover_url,
            rating=rating,
            rating_scale=rating_scale,
            search_index=search_index,
        )

        db.add(post)
        db.flush()  # commit 대신 flush (ID 생성만)

        return post

    def update(self, db: Session, post: Post, **fields) -> Post:
        for k, v in fields.items():
            setattr(post, k, v)
        db.commit()
        db.refresh(post)
        return post

    def set_status(self, db: Session, post_id: str, status: str) -> Optional[Post]:
        post = self.get_by_id(db, post_id)
        if not post:
            return None
        post.status = status
        db.commit()
        db.refresh(post)
        return post

    def delete_by_id(self, db: Session, post_id: str) -> bool:
        post = self.get_by_id(db, post_id)
        if not post:
            return False
        db.delete(post)
        db.commit()
        return True