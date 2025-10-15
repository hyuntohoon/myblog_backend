from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.domain.models import Post, PostId
from app.domain.ports import PostRepo
from .orm import PostORM, PostBodyORM

def _row_to_domain(r: PostORM) -> Post:
    return Post(
        id=PostId(r.id),
        author_id=r.author_id,
        slug=r.slug,
        title=r.title,
        excerpt=r.excerpt,
        status=r.status,
        visibility=r.visibility,
        published_at=r.published_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
        deleted_at=r.deleted_at,
    )

class SqlPostRepo(PostRepo):
    def __init__(self, session: Session):
        self.session = session

    def ensure_unique_slug(self, desired: str) -> str:
        base = desired
        slug = desired
        i = 2
        while self.session.execute(select(func.count()).select_from(PostORM).where(PostORM.slug == slug)).scalar_one():
            slug = f"{base}-{i}"
            i += 1
        return slug

    def add(self, post: Post, *, content_md: str | None = None, content_html: str | None = None) -> Post:
        row = PostORM(
            author_id=post.author_id,
            slug=post.slug,
            title=post.title,
            excerpt=post.excerpt,
            status=post.status,
            visibility=post.visibility,
        )
        self.session.add(row)
        self.session.flush()
        if content_md or content_html:
            body = PostBodyORM(post_id=row.id, content_md=content_md, content_html=content_html)
            self.session.add(body)
        self.session.flush()
        self.session.refresh(row)
        return _row_to_domain(row)

    def get(self, post_id: PostId):
        r = self.session.get(PostORM, post_id.value)
        return _row_to_domain(r) if r else None

    def get_by_slug(self, slug: str):
        r = self.session.execute(select(PostORM).where(PostORM.slug == slug)).scalars().first()
        return _row_to_domain(r) if r else None

    def list_published(self, limit: int = 50):
        q = (
            select(PostORM)
            .where(PostORM.status == "published", PostORM.deleted_at.is_(None))
            .order_by(PostORM.published_at.desc().nullslast())
            .limit(limit)
        )
        for r in self.session.execute(q).scalars():
            yield _row_to_domain(r)
