# app/models/artist.py
import uuid
from datetime import datetime
from typing import List, Optional, Dict

from sqlalchemy import (
    Text, Integer, BigInteger, DateTime, JSON, Table, Column, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from app.db.base import Base

# 🔗 posts ↔ artists
post_artists = Table(
    "post_artists",
    Base.metadata,
    Column(
        "post_id",
        PGUUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "artist_id",
        PGUUID(as_uuid=True),
        ForeignKey("artists.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    spotify_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    # ... 나머지 컬럼들 (genres, photo_url, popularity 등 기존 그대로)

    # 🔥 Post와 N:N
    posts: Mapped[List["Post"]] = relationship(
        "Post",
        secondary=post_artists,
        back_populates="artists",
        lazy="select",
    )