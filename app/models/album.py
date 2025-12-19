# app/models/album.py
import uuid
from datetime import datetime, date
from typing import Dict, List, Optional

from sqlalchemy import (
    Text, Integer, Date, DateTime, ForeignKey, Table, Column,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import text, Boolean

from app.db.base import Base   # ← SQLAlchemy Base


# 🔗 posts ↔ albums
post_albums = Table(
    "post_albums",
    Base.metadata,
    Column(
        "post_id",
        PGUUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "album_id",
        PGUUID(as_uuid=True),
        ForeignKey("albums.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "is_classic",
        Boolean,
        nullable=False,
        server_default="false",
    ),
)


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    release_date: Mapped[Optional[date]] = mapped_column(Date)
    cover_url: Mapped[Optional[str]] = mapped_column(Text)
    album_type: Mapped[Optional[str]] = mapped_column(Text)

    spotify_id: Mapped[Optional[str]] = mapped_column(Text, unique=True, index=True)

    ext_refs: Mapped[Dict] = mapped_column(JSONB, default=dict)

    total_tracks: Mapped[Optional[int]] = mapped_column(Integer)
    label: Mapped[Optional[str]] = mapped_column(Text)
    popularity: Mapped[Optional[int]] = mapped_column(Integer)

    views: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))

    posts: Mapped[List["Post"]] = relationship(
        "Post",
        secondary=post_albums,
        back_populates="albums",
        lazy="select",
    )