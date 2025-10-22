from fastapi import APIRouter
from app.api.schemas import MetricsBatchRequest, MetricsBatchResponse, PostMetrics
from app.repositories.metrics_repository import InMemoryMetricsRepository

router = APIRouter()
_metrics_repo = InMemoryMetricsRepository()

@router.post("", response_model=MetricsBatchResponse)
def batch_metrics(req: MetricsBatchRequest):
    out = {}
    for slug in req.slugs:
        m = _metrics_repo.get(slug)
        out[slug] = PostMetrics(likes=m["likes"], comments=m["comments"])
    return MetricsBatchResponse(data=out)
