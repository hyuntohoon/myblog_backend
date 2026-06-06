# ✅ 구현 클래스만 두세요. (Protocol 쓰지 않기)
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from myblog_shared_db.models import Section


class SectionRepository:
    """sections 테이블 전용 리포지토리 (STAB-5: 큐레이션된 공개 택소노미).

    Section은 마이그레이션 V13으로 시드되며 get-or-create / 공개 생성 경로가
    없다. 글 작성 시 시드되지 않은 섹션 이름은 거부된다 (no upsert).
    """

    def list_all(self, db: Session) -> List[Section]:
        rows = db.execute(text("""
            SELECT id, name, slug, created_at
            FROM sections ORDER BY id ASC
        """)).fetchall()
        return [
            Section(id=r.id, name=r.name, slug=r.slug, created_at=r.created_at)
            for r in rows
        ]

    def list_names(self, db: Session) -> List[str]:
        rows = db.execute(text("SELECT name FROM sections ORDER BY name ASC")).fetchall()
        return [r[0] for r in rows]

    def get_by_name(self, db: Session, name: str) -> Optional[Section]:
        row = db.execute(text("""
            SELECT id, name, slug, created_at
            FROM sections WHERE name=:name LIMIT 1
        """), {"name": name}).fetchone()
        if not row:
            return None
        return Section(id=row.id, name=row.name, slug=row.slug, created_at=row.created_at)
