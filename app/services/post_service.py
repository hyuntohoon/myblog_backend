# app/services/post_service.py
from __future__ import annotations

from datetime import date
from typing import List, Optional
import re

from sqlalchemy.orm import Session

from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from myblog_shared_db.models import (
    Post, Artist, Track,
    post_albums_table as post_albums,
    post_recommended_tracks_table as post_recommended_tracks,
)


def slugify_title(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "untitled"


class PostService:
    """
    - 카테고리: 이름이 오면 get_or_create로 upsert (없으면 생성)
    - 슬러그: 중복 시 '-2', '-3' … 자동 suffix 부여로 유니크 보장
    - 트랜잭션: service 레벨에서 한 번에 commit
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
        body_mdx: Optional[str] = None,
        posted_date: date,
        status: str = "published",
        category_name: Optional[str] = None,
        album_ids: Optional[list[str]] = None,
        artist_ids: Optional[list[str]] = None,
        album_cover_url: Optional[str] = None,
        rating: Optional[float] = None,
        rating_scale: int = 5,
        album_classics: Optional[dict[str, bool]] = None,
        recommended_tracks: Optional[list[dict]] = None,
    ) -> Post:
        album_ids = album_ids or []
        artist_ids = artist_ids or []
        album_classics = album_classics or {}
        recommended_tracks = recommended_tracks or []

        # 1) 카테고리 처리
        category_name = (category_name or "").strip() or "default"
        cat = self.category_repo.get_by_name(db, category_name)
        if not cat:
            cat = self.category_repo.create(db, category_name)
        category_id = cat.id

        # 2) 슬러그 유니크 보장
        base_slug = slugify_title(title)
        slug = self._ensure_unique_slug(db, base_slug)

        # 3) 평점-only면 search_index = False
        search_index = body_mdx is not None and len(body_mdx.strip()) > 0

        # 4) 포스트 생성 (flush만, commit은 마지막에)
        post = self.post_repo.create(
            db,
            slug=slug,
            title=title.strip(),
            description=description or "",
            body_mdx=body_mdx,
            posted_date=posted_date,
            status=status,
            category_id=category_id,
            album_cover_url=album_cover_url,
            rating=rating,
            rating_scale=rating_scale,
            search_index=search_index,
        )

        # 5) 앨범 연결 (is_classic 포함)
        unique_album_ids = list({aid for aid in album_ids if aid})
        for album_id in unique_album_ids:
            db.execute(
                post_albums.insert().values(
                    post_id=post.id,
                    album_id=album_id,
                    is_classic=album_classics.get(album_id, False),
                )
            )

        # 6) 아티스트 연결
        unique_artist_ids = list({aid for aid in artist_ids if aid})
        if unique_artist_ids:
            artists = db.query(Artist).filter(Artist.id.in_(unique_artist_ids)).all()
            for ar in artists:
                post.artists.append(ar)

        # 7) 추천 트랙 저장 (앱 레벨 검증 포함)
        for rt in recommended_tracks:
            track_id = rt.get("track_id")
            album_id = rt.get("album_id")

            # 앱 레벨 검증: track이 album 소속인지
            track = db.query(Track).filter(Track.id == track_id).first()
            if not track:
                raise ValueError(f"Track {track_id} not found")
            if str(track.album_id) != album_id:
                raise ValueError(
                    f"Track {track_id} does not belong to album {album_id}"
                )

            # 해당 album이 post_albums에 연결되어 있는지 확인
            if album_id not in unique_album_ids:
                raise ValueError(
                    f"Album {album_id} is not linked to this post"
                )

            db.execute(
                post_recommended_tracks.insert().values(
                    post_id=post.id,
                    album_id=album_id,
                    track_id=track_id,
                    position=rt.get("position"),
                    note=rt.get("note"),
                )
            )

        # 8) 트랜잭션 커밋 (한 번에)
        db.commit()
        db.refresh(post)

        return post

    def list(self, db: Session, status: Optional[str] = None) -> List[Post]:
        return self.post_repo.list_by_status(db, status)

    def get_by_id(self, db: Session, post_id: str) -> Optional[Post]:
        return self.post_repo.get_by_id(db, post_id)

    def update(self, db: Session, post_id: str, **fields) -> Optional[Post]:
        post = self.post_repo.get_by_id(db, post_id)
        if not post:
            return None
        return self.post_repo.update(db, post, **fields)

    def delete(self, db: Session, post_id: str) -> bool:
        return self.post_repo.delete_by_id(db, post_id)

    def _ensure_unique_slug(self, db: Session, base: str) -> str:
        if not self.post_repo.get_by_slug(db, base):
            return base

        i = 2
        while True:
            candidate = f"{base}-{i}"
            if not self.post_repo.get_by_slug(db, candidate):
                return candidate
            i += 1