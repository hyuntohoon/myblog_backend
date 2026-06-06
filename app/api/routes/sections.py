# app/api/routes/sections.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import SectionItem, SectionListResponse
from app.db.session import get_db
from app.di import get_section_service
from app.services.section_service import SectionService

router = APIRouter()


@router.get("", response_model=SectionListResponse)
def list_sections(
        db: Session = Depends(get_db),
        svc: SectionService = Depends(get_section_service),
):
    # STAB-5: 읽기 전용. 섹션은 마이그레이션으로 시드되며 생성 엔드포인트가
    # 없다 (비인증 POST /api/categories 구멍은 제거됨).
    return SectionListResponse(
        sections=[SectionItem(**s) for s in svc.list_sections(db)]
    )
