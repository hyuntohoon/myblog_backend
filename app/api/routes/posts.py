# app/api/routes/posts.py
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    PostDetailResponse,
    PostListItem,
    PostListResponse,
    UpdatePostRequest,
    WritePostRequest,
    WritePostResponse,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_post_service
from app.services.post_service import DuplicateSlugError, PostService

router = APIRouter()


@router.get("", response_model=PostListResponse)
def list_posts(
    status: Optional[str] = Query(default=None),
    include_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    posts = svc.list(db, status=status, include_archived=include_archived)
    items = [
        PostListItem(
            id=str(p.id),
            slug=p.slug,
            title=p.title,
            description=p.description or "",
            status=p.status,
            posted_date=p.posted_date,
            rating=p.rating,
            category=p.category.name if p.category else None,
        )
        for p in posts
    ]
    return PostListResponse(posts=items)


@router.post(
    "",
    response_model=WritePostResponse,
    responses={409: {"description": "Slug derived from title already exists"}},
)
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
            subject_best_new=req.subject_best_new,
        )

        return WritePostResponse(id=str(post.id), slug=post.slug)

    except DuplicateSlugError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=f"DB constraint violation: {e.orig}")


@router.get("/{post_id}", response_model=PostDetailResponse)
def get_post(
    post_id: str,
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    post = svc.get_by_id(db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    # FEAT-writer-lowfreq-redesign Step 5: surface the subject album's BEST
    # NEW flag so the writer's edit flow can seed its toggle. Only meaningful
    # when the post has exactly one album — otherwise return null.
    subject_best_new = None
    if len(post.albums) == 1:
        subject_best_new = bool(getattr(post.albums[0], "best_new", False))

    return PostDetailResponse(
        id=str(post.id),
        slug=post.slug,
        title=post.title,
        description=post.description or "",
        body_mdx=post.body_mdx,
        status=post.status,
        posted_date=post.posted_date,
        rating=post.rating,
        category=post.category.name if post.category else None,
        album_ids=[str(a.id) for a in post.albums],
        artist_ids=[str(a.id) for a in post.artists],
        recommended_tracks=svc.list_recommended_tracks(db, post.id),
        subject_best_new=subject_best_new,
    )


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


@router.delete(
    "/{post_id}",
    responses={
        200: {"description": "Soft-archived (status='archived'); body echoes new status."},
        204: {"description": "Hard-deleted; no body."},
        404: {"description": "Post not found"},
    },
)
def delete_post(
    post_id: str,
    hard: bool = Query(default=False),
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    result = svc.delete(db, post_id, hard=hard)
    if hard:
        if not result:
            raise HTTPException(status_code=404, detail="Post not found")
        return Response(status_code=204)
    if result is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"id": str(result.id), "status": result.status}


@router.patch("/{post_id}/restore")
def restore_post(
    post_id: str,
    db: Session = Depends(get_db),
    svc: PostService = Depends(get_post_service),
    _claims: Dict = Depends(require_cognito_token),
):
    post = svc.restore(db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"id": str(post.id), "status": post.status}