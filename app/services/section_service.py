# app/services/section_service.py
from typing import List

from sqlalchemy import text


class SectionService:
    """시드된 섹션 택소노미에 대한 읽기 전용 접근 (STAB-5).

    생성/업서트 없음: 섹션은 마이그레이션 V13으로 시드되며, 비인증 공개 생성
    경로(`POST /api/categories`)는 STAB-2 P0로 제거됐다.
    """

    def list_sections(self, db) -> List[dict]:
        rows = db.execute(
            text("SELECT name, slug FROM sections ORDER BY id")
        ).fetchall()
        return [{"name": r.name, "slug": r.slug} for r in rows]

    def list_names(self, db) -> List[str]:
        rows = db.execute(
            text("SELECT name FROM sections ORDER BY name")
        ).fetchall()
        return [r[0] for r in rows]
