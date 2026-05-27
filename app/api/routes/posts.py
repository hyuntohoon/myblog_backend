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
        category_name = (req.category or "default").strip()

        # 추천 트랙 dict 변환
        recommended_tracks = [
            {
                "album_id": rt.album_id,
                "track_id": rt.track_id,
                "position": rt.position,
                "note": rt.note,
            }
            for rt in req.recommended_tracks
        ]

        post = svc.create(
            db,
            title=req.title,
            description=req.description,
            body_mdx=req.body_mdx,
            posted_date=req.posted_date,
            status=req.status,
            category_name=category_name,
            album_ids=req.album_ids,
            artist_ids=req.artist_ids,
            rating=req.rating,
            rating_scale=5,
            album_classics=req.album_classics,
            recommended_tracks=recommended_tracks,
        )

        return WritePostResponse(id=str(post.id), slug=post.slug)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))