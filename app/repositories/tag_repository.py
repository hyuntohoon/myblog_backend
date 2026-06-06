# ✅ 구현 클래스만 두세요. (Protocol 쓰지 않기)
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from myblog_shared_db.models import Tag


class TagRepository:
    """tags 테이블 전용 리포지토리 (STAB-5 Step 4: 리뷰 태그 M:N).

    Tag는 데이터 시드로 채워지며(마이그레이션 아님) get-or-create / 공개 생성
    경로가 없다. 글 작성 시 시드되지 않은 태그 이름은 거부된다 (SectionRepository
    의 reject-unknown 정책 미러).
    """

    def list_all(self, db: Session) -> List[Tag]:
        return list(db.execute(select(Tag).order_by(Tag.id.asc())).scalars())

    def get_by_name(self, db: Session, name: str) -> Optional[Tag]:
        return db.execute(
            select(Tag).where(Tag.name == name)
        ).scalar_one_or_none()

    def get_many_by_names(self, db: Session, names: List[str]) -> List[Tag]:
        """Resolve a list of tag names to Tag rows (order not preserved).

        Returns only the rows that exist; the caller diffs against the input to
        detect unknown names and reject them (no get-or-create).
        """
        if not names:
            return []
        return list(
            db.execute(select(Tag).where(Tag.name.in_(names))).scalars()
        )
