# app/services/post_service.py
from __future__ import annotations

from datetime import date
from typing import List, Optional, Union
import re

from sqlalchemy.orm import Session

from app.repositories.post_repository import PostRepository
from app.repositories.section_repository import SectionRepository
from app.repositories.tag_repository import TagRepository
from myblog_shared_db.models import (
    Album, Post, Artist, Track, ReviewBucketItem,
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
    - 섹션(STAB-5): 이름이 오면 시드된 섹션에서 조회, 없으면 거부 (get-or-create 제거)
    - 슬러그: 제목에서 생성, 중복이면 DuplicateSlugError (자동 suffix 없음)
    - 트랜잭션: service 레벨에서 한 번에 commit
    """

    def __init__(
        self,
        post_repo: PostRepository,
        section_repo: SectionRepository,
        tag_repo: Optional[TagRepository] = None,
    ):
        self.post_repo = post_repo
        self.section_repo = section_repo
        # tag_repo added in STAB-5 Step 4; defaulted so the many existing
        # construction sites (and tests) need no churn. DI injects it explicitly.
        self.tag_repo = tag_repo or TagRepository()

    def _resolve_tags(self, db: Session, names) -> list:
        """Resolve seeded tag names → Tag rows; reject unknown (no get-or-create).

        Mirrors the section reject-unknown policy. Order/dupes don't matter
        (M:N set). Raises ValueError on any unknown name (route → 400).
        """
        clean = list(dict.fromkeys(
            n.strip() for n in (names or []) if isinstance(n, str) and n.strip()
        ))
        if not clean:
            return []
        rows = self.tag_repo.get_many_by_names(db, clean)
        found = {t.name for t in rows}
        unknown = [n for n in clean if n not in found]
        if unknown:
            raise ValueError(
                "unknown tag(s): " + ", ".join(repr(u) for u in unknown)
            )
        return rows

    def create(
        self,
        db: Session,
        *,
        title: str,
        description: str = "",
        body_mdx: Optional[str] = None,
        posted_date: date,
        status: str = "published",
        section_name: Optional[str] = None,
        tags: Optional[list[str]] = None,
        album_ids: Optional[list[str]] = None,
        artist_ids: Optional[list[str]] = None,
        album_cover_url: Optional[str] = None,
        rating: Optional[float] = None,
        rating_scale: int = 5,
        album_classics: Optional[dict[str, bool]] = None,
        recommended_track_ids: Optional[list[str]] = None,
        subject_best_new: Optional[bool] = None,
    ) -> Post:
        album_ids = album_ids or []
        artist_ids = artist_ids or []
        album_classics = album_classics or {}
        recommended_track_ids = recommended_track_ids or []

        # 1) 섹션 처리 — 시드된 섹션만 허용 (get-or-create 제거, unknown 거부)
        section_name = (section_name or "").strip()
        if section_name:
            sec = self.section_repo.get_by_name(db, section_name)
            if not sec:
                raise ValueError(f"unknown section: {section_name!r}")
            section_id = sec.id
        else:
            section_id = None

        # 1b) 태그 처리 — 시드된 태그만 허용 (섹션과 동일 reject-unknown 정책).
        # 이름→Tag 해석을 INSERT 전에 끝내 unknown이면 글을 만들지 않고 거부.
        tag_objs = self._resolve_tags(db, tags)

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
            section_id=section_id,
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

        # 6b) 리뷰 태그 연결 (M:N). tag_objs는 1b에서 이미 검증됨 (unknown 거부).
        if tag_objs:
            post.tags = tag_objs

        # 7) 추천 트랙 저장 — set of picked track IDs. album_id is resolved
        #    from tracks.album_id and validated against the post's linked albums.
        #    `position` column kept nullable; never written (RFC Step 3).
        for track_id in dict.fromkeys(tid for tid in recommended_track_ids if tid):
            track = db.query(Track).filter(Track.id == track_id).first()
            if not track:
                raise ValueError(f"Track {track_id} not found")
            album_id = str(track.album_id)
            if album_id not in unique_album_ids:
                raise ValueError(
                    f"Track {track_id}'s album {album_id} is not linked to this post"
                )
            db.execute(
                post_recommended_tracks.insert().values(
                    post_id=post.id,
                    album_id=album_id,
                    track_id=track_id,
                )
            )

        # FEAT-writer-lowfreq-redesign Step 5: editor-set BEST NEW on the
        # single-subject album. Same transaction as the post insert (one auth
        # gate, atomic). Silent no-op when zero or many albums are linked —
        # the badge is per-album and only meaningful with one subject.
        if subject_best_new is not None and len(unique_album_ids) == 1:
            db.query(Album).filter(Album.id == unique_album_ids[0]).update(
                {"best_new": bool(subject_best_new)}, synchronize_session=False
            )

        # 9) 트랜잭션 커밋 (한 번에)
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
        # JSON field stays `category` (contract rename deferred to Step 5);
        # it resolves to the renamed `section_id` FK.
        section_name = fields.pop("category", _MISSING)
        tags = fields.pop("tags", _MISSING)
        album_ids = fields.pop("album_ids", _MISSING)
        artist_ids = fields.pop("artist_ids", _MISSING)
        recommended_track_ids = fields.pop("recommended_track_ids", _MISSING)
        subject_best_new = fields.pop("subject_best_new", _MISSING)

        if section_name is not _MISSING:
            if isinstance(section_name, str) and section_name.strip():
                name = section_name.strip()
                sec = self.section_repo.get_by_name(db, name)
                if not sec:
                    raise ValueError(f"unknown section: {name!r}")
                fields["section_id"] = sec.id
            else:
                fields["section_id"] = None

        # Resolve tag names BEFORE the column update so an unknown tag rejects
        # the whole edit (no partial write). Empty list = explicit clear.
        tag_objs = _MISSING if tags is _MISSING else self._resolve_tags(db, tags)

        if fields:
            post = self.post_repo.update(db, post, **fields)

        if tag_objs is not _MISSING:
            post.tags = tag_objs
            db.commit()
            db.refresh(post)

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

        # FEAT-writer-lowfreq-redesign Step 5: same single-subject UPDATE
        # pattern as create(). Read the post's currently-linked albums after
        # any album_ids replacement above so the count matches the live shape.
        if subject_best_new is not None and subject_best_new is not _MISSING:
            current_album_ids = [str(a.id) for a in post.albums]
            if len(current_album_ids) == 1:
                db.query(Album).filter(Album.id == current_album_ids[0]).update(
                    {"best_new": bool(subject_best_new)}, synchronize_session=False
                )
                db.commit()
                db.refresh(post)

        if recommended_track_ids is not _MISSING:
            # Replace pattern: clear existing rows, insert new picks.
            # Validation mirrors create(): track exists + its album is linked.
            db.execute(
                post_recommended_tracks.delete().where(
                    post_recommended_tracks.c.post_id == post.id
                )
            )
            linked_album_ids = {str(a.id) for a in post.albums}
            for track_id in dict.fromkeys(tid for tid in (recommended_track_ids or []) if tid):
                track = db.query(Track).filter(Track.id == track_id).first()
                if not track:
                    raise ValueError(f"Track {track_id} not found")
                album_id = str(track.album_id)
                if album_id not in linked_album_ids:
                    raise ValueError(
                        f"Track {track_id}'s album {album_id} is not linked to this post"
                    )
                db.execute(
                    post_recommended_tracks.insert().values(
                        post_id=post.id,
                        album_id=album_id,
                        track_id=track_id,
                    )
                )
            db.commit()
            db.refresh(post)

        return post

    def list_recommended_track_ids(self, db: Session, post_id: str) -> list[str]:
        """Return picked track IDs for a post (set semantics; insertion order)."""
        from sqlalchemy import select

        rows = db.execute(
            select(post_recommended_tracks.c.track_id)
            .where(post_recommended_tracks.c.post_id == post_id)
        ).all()
        return [str(r.track_id) for r in rows]

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
            # D22 (FEAT-member-dashboard Step 5): review_bucket_items.post_id is
            # ON DELETE SET NULL, so a hard post delete would otherwise orphan the
            # bucket item pointing at it. Detach those rows in the SAME transaction,
            # BEFORE deleting the post (delete_by_id commits once for both). Hard
            # path only — soft delete (archive) must NOT touch bucket items.
            db.query(ReviewBucketItem).filter(
                ReviewBucketItem.post_id == post_id
            ).delete(synchronize_session=False)
            return self.post_repo.delete_by_id(db, post_id)
        return self.post_repo.set_status(db, post_id, "archived")