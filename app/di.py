# app/di.py
from app.services.post_service import PostService
from app.services.category_service import CategoryService
from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from app.services.post_service import PostService

def get_category_service() -> CategoryService:
    return CategoryService()

def get_post_service():
    post_repo = PostRepository()
    category_repo = CategoryRepository()
    return PostService(post_repo, category_repo)