# app/services/tag_service.py
from typing import List

from sqlalchemy.orm import Session

from app.repositories.tag_repository import TagRepository


class TagService:
    """시드된 리뷰 태그 어휘에 대한 읽기 전용 접근 (STAB-5 Step 4).

    생성/업서트 없음: 태그는 데이터 시드로 채워지며 공개 생성 경로가 없다
    (SectionService 미러). 글에 태그를 다는 write는 PostService가 처리한다.
    """

    def __init__(self, tag_repo: TagRepository):
        self.tag_repo = tag_repo

    def list_tags(self, db: Session) -> List[dict]:
        return [
            {"name": t.name, "slug": t.slug}
            for t in self.tag_repo.list_all(db)
        ]
