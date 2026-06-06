# app/api/routes/tags.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import TagItem, TagListResponse
from app.db.session import get_db
from app.di import get_tag_service
from app.services.tag_service import TagService

router = APIRouter()


@router.get("", response_model=TagListResponse)
def list_tags(
        db: Session = Depends(get_db),
        svc: TagService = Depends(get_tag_service),
):
    # STAB-5 Step 4: 읽기 전용. 태그는 데이터 시드로 채워지며 생성 엔드포인트가
    # 없다 (섹션과 동일 정책 — get-or-create / 공개 create 없음).
    return TagListResponse(
        tags=[TagItem(**t) for t in svc.list_tags(db)]
    )
