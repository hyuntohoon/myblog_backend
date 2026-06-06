# app/di.py
from app.services.post_service import PostService
from app.services.section_service import SectionService
from app.services.bucket_service import BucketService
from app.services.library_service import LibraryService
from app.repositories.post_repository import PostRepository
from app.repositories.section_repository import SectionRepository
from app.repositories.metrics_repository import MetricsRepository, SqlMetricsRepository


def get_section_service() -> SectionService:
    return SectionService()


def get_bucket_service() -> BucketService:
    return BucketService()


def get_library_service() -> LibraryService:
    return LibraryService()


def get_sqs_client():
    from app.clients.sqs_client import SqsClient

    return SqsClient()


def get_post_service():
    post_repo = PostRepository()
    section_repo = SectionRepository()
    return PostService(post_repo, section_repo)


_metrics_repo: MetricsRepository = SqlMetricsRepository()


def get_metrics_repository() -> MetricsRepository:
    return _metrics_repo
