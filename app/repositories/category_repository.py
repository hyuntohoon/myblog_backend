# ✅ 구현 클래스만 두세요. (Protocol 쓰지 않기)
from typing import List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models.category import Category
import re

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "category"

class CategoryRepository:
    def list_names(self, db: Session) -> List[str]:
        rows = db.execute(text("SELECT name FROM categories ORDER BY name ASC")).fetchall()
        return [r[0] for r in rows]

    def get_by_name(self, db: Session, name: str) -> Optional[Category]:
        row = db.execute(text("""
            SELECT id, name, slug, created_at
            FROM categories WHERE name=:name LIMIT 1
        """), {"name": name}).fetchone()
        if not row:
            return None
        return Category(id=row.id, name=row.name, slug=row.slug, created_at=row.created_at)

    def create(self, db: Session, name: str) -> Category:
        name = name.strip()
        if not name:
            raise ValueError("empty category name")
        slug = _slugify(name)
        exist = db.execute(
            text("SELECT 1 FROM categories WHERE name=:n OR slug=:s LIMIT 1"),
            {"n": name, "s": slug},
        ).fetchone()
        if exist:
            raise ValueError("category already exists")

        row = db.execute(text("""
            INSERT INTO categories (name, slug)
            VALUES (:name, :slug)
            RETURNING id, name, slug, created_at
        """), {"name": name, "slug": slug}).fetchone()
        db.commit()
        return Category(id=row.id, name=row.name, slug=row.slug, created_at=row.created_at)