# app/di.py
from app.services.post_service import PostService
from app.services.category_service import CategoryService
from app.services.review_service import ReviewService
from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from app.repositories.review_repository import ReviewRepository
from app.repositories.metrics_repository import MetricsRepository, SqlMetricsRepository


def get_category_service() -> CategoryService:
    return CategoryService()


def get_post_service():
    post_repo = PostRepository()
    category_repo = CategoryRepository()
    return PostService(post_repo, category_repo)


def get_review_service() -> ReviewService:
    return ReviewService(ReviewRepository(), PostRepository())


_metrics_repo: MetricsRepository = SqlMetricsRepository()


def get_metrics_repository() -> MetricsRepository:
    return _metrics_repo
