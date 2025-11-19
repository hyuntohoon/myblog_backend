# app/models/post.py
import uuid
import re
from datetime import date, datetime
from typing import Optional, List

from sqlalchemy import (
    Text, Date, DateTime, Integer, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from app.db.base import Base
from app.models.album import post_albums
from dataclasses import dataclass, field

class Post(Base):
    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    slug: Mapped[str] = mapped_column(Text, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    body_mdx: Mapped[str] = mapped_column(Text, nullable=False)
    posted_date: Mapped[date] = mapped_column(Date)
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("now()"), onupdate=text("now()")
    )
    status: Mapped[str] = mapped_column(Text, default="published")

    category_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 🔥 앨범 관계
    albums: Mapped[List["Album"]] = relationship(
        "Album",
        secondary=post_albums,
        back_populates="posts",
        lazy="select",
    )

@dataclass
class PostDraft:
    title: str
    body_mdx: str
    description: str = ""
    posted_date: date = field(default_factory=date.today)
    status: str = "published"
    category_name: Optional[str] = None

    music_review_subject: Optional[str] = None
    review_target_id: Optional[str] = None
    rating: Optional[float] = None

    album_ids: List[str] = []  # POST /posts 시 들어오는 앨범 UUID 목록

    def validate(self):
        if not self.title:
            raise ValueError("title required")
        if not self.body_mdx:
            raise ValueError("body_mdx required")