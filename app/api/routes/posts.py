# app/api/routes/posts.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.schemas import WritePostRequest, WritePostResponse
from app.db.session import get_db
from app.di import get_post_service
from app.services.post_service import PostService

router = APIRouter()

@router.post("", response_model=WritePostResponse)
def create_post(
        req: WritePostRequest,
        db: Session = Depends(get_db),
        svc: PostService = Depends(get_post_service),
):
    try:
        post = svc.create(
            db,
            title=req.title,
            description=req.description,
            body_mdx=req.body_mdx,
            posted_date=req.posted_date,
            status=req.status,
            category_name=req.category,  # ✅ 이름만 보내도 됨 (없으면 생성)
        )
        return WritePostResponse(id=post.id, slug=post.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

