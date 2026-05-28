from typing import Protocol, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session


class MetricsRepository(Protocol):
    def get_many(self, db: Session, slugs: List[str]) -> Dict[str, dict]: ...


class InMemoryMetricsRepository(MetricsRepository):
    """Legacy in-memory store. Kept for tests / local dev fallback."""

    def __init__(self) -> None:
        self._m: Dict[str, dict] = {}

    def get_many(self, db: Session, slugs: List[str]) -> Dict[str, dict]:
        del db
        return {s: self._m.setdefault(s, {"likes": 0, "comments": 0}) for s in slugs}


class SqlMetricsRepository(MetricsRepository):
    """Reads likes / comments_count from post_metrics, joined to posts.slug."""

    _SQL = text(
        """
        SELECT p.slug AS slug,
               COALESCE(m.likes, 0)          AS likes,
               COALESCE(m.comments_count, 0) AS comments
        FROM posts p
        LEFT JOIN post_metrics m ON m.post_id = p.id
        WHERE p.slug = ANY(:slugs)
        """
    )

    def get_many(self, db: Session, slugs: List[str]) -> Dict[str, dict]:
        if not slugs:
            return {}
        rows = db.execute(self._SQL, {"slugs": list(slugs)}).mappings().all()
        found = {r["slug"]: {"likes": int(r["likes"]), "comments": int(r["comments"])} for r in rows}
        for s in slugs:
            found.setdefault(s, {"likes": 0, "comments": 0})
        return found
