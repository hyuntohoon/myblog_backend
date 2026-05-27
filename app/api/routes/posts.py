# app/api/routes/posts.py
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.schemas import (
    PostListItem,
    PostListResponse,
    UpdatePostRequest,
    WritePostRequest,
    WritePostResponse,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_post_service
from app.services.post_service import PostService

router = APIRouter()


@router.get("", response_model=PostListResponse)
def list_posts(
    status: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    posts = svc.list(db, status=status)
    items = [
        PostListItem(
            id=str(p.id),
            slug=p.slug,
            title=p.title,
            description=p.description or "",
            status=p.status,
            posted_date=p.posted_date,
            rating=p.rating,
        )
        for p in posts
    ]
    return PostListResponse(posts=items)


@router.post("", response_model=WritePostResponse)
def create_post(
    req: WritePostRequest,
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
):
    try:
        category_name = (req.category or "default").strip()

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


@router.put("/{post_id}", response_model=WritePostResponse)
def update_post(
    post_id: str,
    req: UpdatePostRequest,
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    updates = req.model_dump(exclude_unset=True)
    post = svc.update(db, post_id, **updates)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return WritePostResponse(id=str(post.id), slug=post.slug)


@router.delete("/{post_id}", status_code=204)
def delete_post(
    post_id: str,
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    deleted = svc.delete(db, post_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Post not found")