# app/di.py
from app.services.post_service import PostService
from app.services.category_service import CategoryService
from app.services.bucket_service import BucketService
from app.repositories.post_repository import PostRepository
from app.repositories.category_repository import CategoryRepository
from app.repositories.metrics_repository import MetricsRepository, SqlMetricsRepository


def get_category_service() -> CategoryService:
    return CategoryService()


def get_bucket_service() -> BucketService:
    return BucketService()


def get_post_service():
    post_repo = PostRepository()
    category_repo = CategoryRepository()
    return PostService(post_repo, category_repo)


_metrics_repo: MetricsRepository = SqlMetricsRepository()


def get_metrics_repository() -> MetricsRepository:
    return _metrics_repo
