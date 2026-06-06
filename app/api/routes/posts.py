# app/api/routes/posts.py
import logging
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
from app.services import content_sync
from app.services.post_service import DuplicateSlugError, PostService

logger = logging.getLogger(__name__)

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
            category=p.section.name if p.section else None,
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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        section_name = (req.category or "").strip() or None

        post = svc.create(
            db,
            title=req.title,
            description=req.description,
            body_mdx=req.body_mdx,
            posted_date=req.posted_date,
            status=req.status,
            section_name=section_name,
            album_ids=req.album_ids,
            artist_ids=req.artist_ids,
            rating=req.rating,
            rating_scale=5,
            album_classics=req.album_classics,
            recommended_track_ids=req.recommended_track_ids,
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
        category=post.section.name if post.section else None,
        album_ids=[str(a.id) for a in post.albums],
        artist_ids=[str(a.id) for a in post.artists],
        recommended_track_ids=svc.list_recommended_track_ids(db, post.id),
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
    try:
        post = svc.update(db, post_id, **updates)
    except ValueError as e:
        # unknown section (no get-or-create) → reject cleanly, not a 500
        raise HTTPException(status_code=400, detail=str(e))
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
    # Capture the content coordinates before a hard delete removes the row —
    # un-publishing the static MDX needs slug + posted_date.
    existing = svc.get_by_id(db, post_id)
    coords = (existing.posted_date, existing.slug) if existing is not None else None

    result = svc.delete(db, post_id, hard=hard)
    if hard:
        if not result:
            raise HTTPException(status_code=404, detail="Post not found")
    elif result is None:
        raise HTTPException(status_code=404, detail="Post not found")

    # FEAT-post-edit-delete-ui Step 3: archive and hard delete both un-publish
    # the static page. Idempotent for never-published posts (drafts). The DB op
    # already committed; on a GitHub failure we surface 502 without rolling it
    # back (the post is genuinely un-published in the DB — only cleanup failed).
    if coords is not None:
        try:
            content_sync.remove_post_content(coords[0], coords[1])
        except RuntimeError as e:
            logger.error("MDX removal failed for post %s: %s", post_id, e)
            raise HTTPException(
                status_code=502,
                detail=f"Post {'deleted' if hard else 'archived'} but static page removal failed: {e}",
            )

    if hard:
        return Response(status_code=204)
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

    # FEAT-post-edit-delete-ui Step 3: restore is the symmetric inverse of
    # archive — archive removed the MDX, so restore must re-publish it, else the
    # post is 'published' in the DB but its static page 404s. Re-derived from the
    # row. A GitHub failure surfaces 502 (idempotent + retryable: restore re-runs
    # set_status + re-publish safely).
    try:
        rec_track_ids = svc.list_recommended_track_ids(db, post.id)
        content_sync.republish_post_content(db, post, rec_track_ids)
    except RuntimeError as e:
        logger.error("MDX re-publish failed for post %s: %s", post_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"Post restored but static page re-publish failed: {e}",
        )

    return {"id": str(post.id), "status": post.status}