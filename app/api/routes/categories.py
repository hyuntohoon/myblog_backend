# app/api/routes/categories.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.schemas import CategoryListResponse, AddCategoryRequest
from app.db.session import get_db
from app.di import get_category_service
from app.services.category_service import CategoryService

router = APIRouter()  # ✅ 이 줄이 꼭 있어야 함

@router.get("", response_model=CategoryListResponse)
def list_categories(
        db: Session = Depends(get_db),
        svc: CategoryService = Depends(get_category_service),
):
    return CategoryListResponse(categories=svc.list_names(db))

@router.post("")
def add_category(
        req: AddCategoryRequest,
        db: Session = Depends(get_db),
        svc: CategoryService = Depends(get_category_service),
):
    try:
        cat = svc.add(db, req.name)
        return {"ok": True, "id": cat.id, "name": cat.name, "slug": cat.slug}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))