# ✅ 구현 클래스만 두세요. (Protocol 쓰지 않기)
from typing import List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models.post import Post


class PostRepository:
    def list_all(self, db: Session) -> List[Post]:
        rows = db.execute(text("""
            SELECT id, slug, title, description, body_mdx, posted_date, last_updated_at, status, category_id
            FROM posts
            ORDER BY posted_date DESC
        """)).fetchall()
        return [Post(*r) for r in rows]

    def get_by_slug(self, db: Session, slug: str) -> Optional[Post]:
        r = db.execute(text("""
            SELECT id, slug, title, description, body_mdx, posted_date, last_updated_at, status, category_id
            FROM posts
            WHERE slug = :slug
            LIMIT 1
        """), {"slug": slug}).fetchone()
        return Post(*r) if r else None

    def create(
            self,
            db: Session,
            *,
            slug: str,
            title: str,
            description: str,
            body_mdx: str,
            posted_date,
            status: str,
            category_id,
    ) -> Post:
        # ✅ id는 DEFAULT(gen_random_uuid()) 사용 → INSERT/바인드에서 제외
        row = db.execute(
            text("""
                INSERT INTO posts (slug, title, description, body_mdx, posted_date, status, category_id)
                VALUES (:slug, :title, :description, :body_mdx, :posted_date, :status, :category_id)
                RETURNING id, slug, title, description, body_mdx, posted_date, last_updated_at, status, category_id
            """),
            {
                "slug": slug,
                "title": title,
                "description": description,
                "body_mdx": body_mdx,
                "posted_date": posted_date,
                "status": status,
                "category_id": category_id,
            },
        ).fetchone()

        db.commit()

        # ✅ row의 UUID를 문자열로 변환
        data = dict(row._mapping)
        if not isinstance(data["id"], str):
            data["id"] = str(data["id"])

        return Post(**data)