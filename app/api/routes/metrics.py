from fastapi import APIRouter, Depends

from app.api.schemas import MetricsBatchRequest, MetricsBatchResponse, PostMetrics
from app.db.session import get_db
from app.di import get_metrics_repository
from app.repositories.metrics_repository import MetricsRepository

router = APIRouter()


@router.post("", response_model=MetricsBatchResponse)
def batch_metrics(
    req: MetricsBatchRequest,
    db=Depends(get_db),
    repo: MetricsRepository = Depends(get_metrics_repository),
):
    rows = repo.get_many(db, req.slugs)
    return MetricsBatchResponse(
        data={slug: PostMetrics(likes=m["likes"], comments=m["comments"]) for slug, m in rows.items()}
    )
