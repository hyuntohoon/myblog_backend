from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Literal

PostStatus = Literal["draft", "published", "archived"]
Visibility = Literal["public", "private", "unlisted"]

@dataclass(frozen=True)
class PostId:
    value: int

@dataclass
class Post:
    id: Optional[PostId]
    author_id: int
    slug: str
    title: str
    excerpt: Optional[str] = None
    status: PostStatus = "draft"
    visibility: Visibility = "public"
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

@dataclass
class PostBody:
    post_id: int
    content_md: Optional[str] = None
    content_html: Optional[str] = None
    updated_at: Optional[datetime] = None
