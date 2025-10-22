# app/services/category_service.py
from sqlalchemy import text
from slugify import slugify

class CategoryService:
    def list_names(self, db) -> list[str]:
        rows = db.execute(
            text("SELECT name FROM categories ORDER BY name")
        ).fetchall()
        return [r[0] for r in rows]

    def add(self, db, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")
        slug = slugify(name)

        exists = db.execute(
            text("SELECT id, name, slug FROM categories WHERE slug=:slug"),
            {"slug": slug},
        ).fetchone()
        if exists:
            raise ValueError("category already exists")

        row = db.execute(
            text("""
                INSERT INTO categories (name, slug)
                VALUES (:name, :slug)
                RETURNING id, name, slug
            """),
            {"name": name, "slug": slug},
        ).fetchone()
        db.commit()
        # 간단 DTO
        return type("Category", (), {"id": row[0], "name": row[1], "slug": row[2]})

    # ✅ B안 핵심: 없으면 만들고, 있으면 기존 id 반환
    def get_or_create(self, db, name: str) -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        slug = slugify(name)

        found = db.execute(
            text("SELECT id FROM categories WHERE slug=:slug"),
            {"slug": slug},
        ).fetchone()
        if found:
            return found[0]

        row = db.execute(
            text("""
                INSERT INTO categories (name, slug)
                VALUES (:name, :slug)
                RETURNING id
            """),
            {"name": name, "slug": slug},
        ).fetchone()
        db.commit()
        return row[0]