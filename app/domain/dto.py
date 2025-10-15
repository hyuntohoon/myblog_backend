from pydantic import BaseModel, Field
from typing import Optional, Literal

class PostCreateDTO(BaseModel):
    title: str = Field(min_length=1)
    excerpt: Optional[str] = None
    content_md: Optional[str] = None
    visibility: Literal["public", "private", "unlisted"] = "public"
    slug: Optional[str] = None  # optional; auto-generate if not provided

class PostDTO(BaseModel):
    id: int
    author_id: int
    slug: str
    title: str
    excerpt: Optional[str]
    status: Literal["draft", "published", "archived"]
    visibility: Literal["public", "private", "unlisted"]
