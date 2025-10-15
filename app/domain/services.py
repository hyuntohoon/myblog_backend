import re
from .dto import PostCreateDTO, PostDTO
from .models import Post, PostId
from .ports import UnitOfWork
from .errors import NotFound

_slug_re = re.compile(r"[^a-z0-9-]+")

def slugify(s: str) -> str:
    s = s.strip().lower().replace(" ", "-")
    s = _slug_re.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "post"

class PostService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow

    def create(self, author_id: int, data: PostCreateDTO) -> PostDTO:
        slug = data.slug or slugify(data.title)
        slug = self.uow.posts.ensure_unique_slug(slug)

        post = Post(
            id=None,
            author_id=author_id,
            slug=slug,
            title=data.title.strip(),
            excerpt=(data.excerpt or "").strip() or None,
            status="draft",
            visibility=data.visibility,
        )
        created = self.uow.posts.add(post, content_md=data.content_md)
        self.uow.commit()
        return PostDTO(
            id=created.id.value,
            author_id=created.author_id,
            slug=created.slug,
            title=created.title,
            excerpt=created.excerpt,
            status=created.status,
            visibility=created.visibility,
        )

    def get(self, post_id: int) -> PostDTO:
        p = self.uow.posts.get(PostId(post_id))
        if not p:
            raise NotFound("post not found")
        return PostDTO(
            id=p.id.value,
            author_id=p.author_id,
            slug=p.slug,
            title=p.title,
            excerpt=p.excerpt,
            status=p.status,
            visibility=p.visibility,
        )

    def list_published(self, limit: int = 50) -> list[PostDTO]:
        return [PostDTO(
            id=p.id.value,
            author_id=p.author_id,
            slug=p.slug,
            title=p.title,
            excerpt=p.excerpt,
            status=p.status,
            visibility=p.visibility,
        ) for p in self.uow.posts.list_published(limit)]
