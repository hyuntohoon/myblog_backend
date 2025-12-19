# app/models/post.py
import uuid
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from sqlalchemy.types import Numeric

from sqlalchemy import (
    Text,
    Date,
    DateTime,
    BigInteger,
    Boolean,
    ForeignKey,
    SmallInteger,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB, ENUM as PGEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text as sa_text

from app.db.base import Base
from app.models.album import post_albums
from app.models.artist import post_artists
from app.models.category import Category

POST_STATUS = PGEnum(
    "draft",
    "published",
    "archived",
    name="post_status",
    create_type=False,
)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )

    slug: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="",
    )

    body_mdx: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # NULL 허용

    body_text: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    posted_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=sa_text("now()"),
    )

    status: Mapped[str] = mapped_column(
        POST_STATUS,
        nullable=False,
        server_default="published",
    )

    category_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    category: Mapped[Optional["Category"]] = relationship(
        "Category",
        back_populates="posts",
        lazy="select",
    )

    search_index: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
    )

    extra: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa_text("'{}'::jsonb"),
    )

    albums: Mapped[List["Album"]] = relationship(
        "Album",
        secondary=post_albums,
        back_populates="posts",
        lazy="select",
    )

    artists: Mapped[List["Artist"]] = relationship(
        "Artist",
        secondary=post_artists,
        back_populates="posts",
        lazy="select",
    )

    album_cover_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    rating: Mapped[Optional[float]] = mapped_column(Numeric(3, 1), nullable=True)

    rating_scale: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default="5",
    )


@dataclass
class PostDraft:
    title: str
    body_mdx: Optional[str] = None
    description: str = ""
    posted_date: date = field(default_factory=date.today)
    status: str = "published"
    category_name: Optional[str] = None

    music_review_subject: Optional[str] = None
    review_target_id: Optional[str] = None
    rating: Optional[float] = None
    rating_scale: int = 5

    album_ids: List[str] = field(default_factory=list)
    artist_ids: List[str] = field(default_factory=list)

    album_cover_url: Optional[str] = None

    def validate(self):
        if not self.title:
            raise ValueError("title required")
        # body_mdx 필수 검증 제거 (평점-only 허용)