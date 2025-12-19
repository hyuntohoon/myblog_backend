# app/models/category.py
import datetime
from typing import List

from sqlalchemy import Integer, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from app.db.base import Base


class Category(Base):
    __tablename__ = "categories"  # 🔥 FK에서 참조하는 이름이랑 같아야 함

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        Text,
        unique=True,
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(
        Text,
        unique=True,
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        server_default=text("now()"),
    )

    # 역방향 관계 (선택)
    posts: Mapped[List["Post"]] = relationship(
        "Post",
        back_populates="category",
        lazy="select",
    )