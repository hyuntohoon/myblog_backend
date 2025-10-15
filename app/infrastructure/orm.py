from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, Text, String, BigInteger, TIMESTAMP, text as sqltext, ForeignKey
from sqlalchemy.dialects.postgresql import ENUM, CITEXT

post_status_enum = ENUM('draft','published','archived', name='post_status', create_type=False)
visibility_enum = ENUM('public','private','unlisted', name='visibility', create_type=False)

class Base(DeclarativeBase): pass

class UserORM(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    username: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String, server_default=sqltext("'user'"))
    is_active: Mapped[bool] = mapped_column(server_default=sqltext("true"))
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=sqltext("now()"))
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=sqltext("now()"))
    deleted_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True))

class PostORM(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(CITEXT, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(post_status_enum, nullable=False, server_default=sqltext("'draft'"))
    visibility: Mapped[str] = mapped_column(visibility_enum, nullable=False, server_default=sqltext("'public'"))
    published_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=sqltext("now()"))
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=sqltext("now()"))
    deleted_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True))

class PostBodyORM(Base):
    __tablename__ = "post_bodies"
    post_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True)
    content_md: Mapped[str | None] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=sqltext("now()"))
