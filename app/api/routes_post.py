from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from sqlalchemy.orm import Session
from app.api.deps import get_session, get_uow
from app.domain.dto import PostCreateDTO, PostDTO
from app.domain.services import PostService
from app.domain.errors import NotFound

router = APIRouter(prefix="/posts", tags=["posts"])

def get_service(session: Session = Depends(get_session)) -> PostService:
    uow = get_uow(session)
    return PostService(uow)

@router.get("/", response_model=list[PostDTO])
def list_posts(
        svc: PostService = Depends(get_service),
        status: Optional[str] = Query(None),
        visibility: Optional[str] = Query(None),
        q: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
):
    return svc.list(status=status, visibility=visibility, q=q, limit=limit, offset=offset)

@router.post("/", response_model=PostDTO, status_code=201)
def create_post(payload: PostCreateDTO, svc: PostService = Depends(get_service)):
    # Demo: author_id=1 하드코딩. 추후 인증 시 주입.
    return svc.create(author_id=1, data=payload)

@router.get("/{post_id}", response_model=PostDTO)
def get_post(post_id: int, svc: PostService = Depends(get_service)):
    try:
        return svc.get(post_id)
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
