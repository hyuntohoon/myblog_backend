# app/services/post_service.py
from __future__ import annotations

from datetime import date
from typing import List, Optional, Union
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


_MISSING = object()  # sentinel so callers can distinguish "not provided" from None


class DuplicateSlugError(Exception):
    """Raised when a new post's title would collide with an existing slug.

    The route layer maps this to HTTP 409 so the frontend can surface the
    backend message verbatim and ask the user to change the title.
    """


class PostService:
    """
    - 카테고리: 이름이 오면 get_or_create로 upsert (없으면 생성)
    - 슬러그: 제목에서 생성, 중복이면 DuplicateSlugError (자동 suffix 없음)
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

        # 2) 슬러그 생성 + 중복 시 hard block
        slug = slugify_title(title)
        if self.post_repo.get_by_slug(db, slug):
            raise DuplicateSlugError(
                f'이미 같은 제목의 글이 있습니다 (slug="{slug}"). 제목을 바꿔주세요.'
            )

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

    def list(
        self,
        db: Session,
        status: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Post]:
        # Default public read: status='published' only. include_archived broadens
        # to draft+published+archived (callers with a Cognito token only).
        if status:
            return self.post_repo.list_by_status(db, status)
        if include_archived:
            return self.post_repo.list_by_status(db, None)
        return self.post_repo.list_by_status(db, "published")

    def get_by_id(self, db: Session, post_id: str) -> Optional[Post]:
        return self.post_repo.get_by_id(db, post_id)

    def update(self, db: Session, post_id: str, **fields) -> Optional[Post]:
        post = self.post_repo.get_by_id(db, post_id)
        if not post:
            return None

        # Pull out fields that don't map to scalar columns. Presence (not value)
        # is what matters — `exclude_unset=True` on the route side already filtered
        # out keys the client didn't send, so any key still in `fields` is an
        # intentional write (including a clear-to-empty / clear-to-null).
        category_name = fields.pop("category", _MISSING)
        album_ids = fields.pop("album_ids", _MISSING)
        artist_ids = fields.pop("artist_ids", _MISSING)
        recommended_tracks = fields.pop("recommended_tracks", _MISSING)

        if category_name is not _MISSING:
            if isinstance(category_name, str) and category_name.strip():
                name = category_name.strip()
                cat = self.category_repo.get_by_name(db, name)
                if not cat:
                    cat = self.category_repo.create(db, name)
                fields["category_id"] = cat.id
            else:
                fields["category_id"] = None

        if fields:
            post = self.post_repo.update(db, post, **fields)

        if album_ids is not _MISSING:
            unique = list({aid for aid in (album_ids or []) if aid})
            db.execute(
                post_albums.delete().where(post_albums.c.post_id == post.id)
            )
            for aid in unique:
                db.execute(
                    post_albums.insert().values(
                        post_id=post.id, album_id=aid, is_classic=False
                    )
                )
            db.commit()
            db.refresh(post)

        if artist_ids is not _MISSING:
            unique = list({aid for aid in (artist_ids or []) if aid})
            artists = (
                db.query(Artist).filter(Artist.id.in_(unique)).all() if unique else []
            )
            post.artists = artists
            db.commit()
            db.refresh(post)

        if recommended_tracks is not _MISSING:
            # Replace pattern: clear existing rows, insert new ones.
            # Validation mirrors create(): track must exist + belong to album,
            # album must be linked to post.
            db.execute(
                post_recommended_tracks.delete().where(
                    post_recommended_tracks.c.post_id == post.id
                )
            )
            linked_album_ids = {str(a.id) for a in post.albums}
            for rt in recommended_tracks or []:
                track_id = rt.get("track_id")
                album_id = rt.get("album_id")
                track = db.query(Track).filter(Track.id == track_id).first()
                if not track:
                    raise ValueError(f"Track {track_id} not found")
                if str(track.album_id) != album_id:
                    raise ValueError(
                        f"Track {track_id} does not belong to album {album_id}"
                    )
                if album_id not in linked_album_ids:
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
            db.commit()
            db.refresh(post)

        return post

    def list_recommended_tracks(self, db: Session, post_id: str) -> list[dict]:
        """Return rows from post_recommended_tracks ordered by position (NULLs last)."""
        from sqlalchemy import select

        rows = db.execute(
            select(
                post_recommended_tracks.c.album_id,
                post_recommended_tracks.c.track_id,
                post_recommended_tracks.c.position,
                post_recommended_tracks.c.note,
            )
            .where(post_recommended_tracks.c.post_id == post_id)
            .order_by(post_recommended_tracks.c.position.asc().nullslast())
        ).all()
        return [
            {
                "album_id": str(r.album_id),
                "track_id": str(r.track_id),
                "position": r.position,
                "note": r.note,
            }
            for r in rows
        ]

    def archive(self, db: Session, post_id: str) -> Optional[Post]:
        return self.post_repo.set_status(db, post_id, "archived")

    def restore(self, db: Session, post_id: str) -> Optional[Post]:
        return self.post_repo.set_status(db, post_id, "published")

    def delete(
        self, db: Session, post_id: str, hard: bool = False
    ) -> Union[Optional[Post], bool]:
        # hard=True: legacy behavior, CASCADE removes M:M rows; returns bool.
        # hard=False: soft delete via status='archived'; returns the updated Post
        # so the route can echo the new status. Mixed return type is intentional;
        # the route layer disambiguates by the `hard` flag it sent.
        if hard:
            return self.post_repo.delete_by_id(db, post_id)
        return self.post_repo.set_status(db, post_id, "archived")