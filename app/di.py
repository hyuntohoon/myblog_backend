# app/di.py
from app.services.post_service import PostService
from app.services.section_service import SectionService
from app.services.tag_service import TagService
from app.services.bucket_service import BucketService
from app.services.library_service import LibraryService
from app.services.playback_service import PlaybackService
from app.services.research_service import ResearchService
from app.services.genre_service import GenreService
from app.services.lyrics_service import LyricsService
from app.services.todays_pick_service import TodaysPickService
from app.services.tracked_artist_service import TrackedArtistService
from app.repositories.post_repository import PostRepository
from app.repositories.section_repository import SectionRepository
from app.repositories.tag_repository import TagRepository
from app.repositories.metrics_repository import MetricsRepository, SqlMetricsRepository


def get_section_service() -> SectionService:
    return SectionService()


def get_tag_service() -> TagService:
    return TagService(TagRepository())


def get_bucket_service() -> BucketService:
    return BucketService()


def get_library_service() -> LibraryService:
    return LibraryService()


def get_playback_service() -> PlaybackService:
    return PlaybackService()


def get_lyrics_service() -> LyricsService:
    return LyricsService()


def get_research_service() -> ResearchService:
    return ResearchService()


def get_genre_service() -> GenreService:
    return GenreService()


def get_todays_pick_service() -> TodaysPickService:
    return TodaysPickService()


def get_tracked_artist_service() -> TrackedArtistService:
    return TrackedArtistService()


def get_sqs_client():
    from app.clients.sqs_client import SqsClient

    return SqsClient()


def get_post_service():
    post_repo = PostRepository()
    section_repo = SectionRepository()
    tag_repo = TagRepository()
    return PostService(post_repo, section_repo, tag_repo)


def get_user_service():
    from app.services.user_service import UserService

    return UserService()


def get_integration_service():
    from app.services.integration_service import IntegrationService

    return IntegrationService()


def get_review_service():
    from app.services.review_service import ReviewService

    return ReviewService()


_metrics_repo: MetricsRepository = SqlMetricsRepository()


def get_metrics_repository() -> MetricsRepository:
    return _metrics_repo
