from typing import Protocol, Dict

class MetricsRepository(Protocol):
    def ensure(self, slug: str) -> None: ...
    def get(self, slug: str) -> dict: ...
    def inc_like(self, slug: str) -> None: ...
    def inc_comment(self, slug: str) -> None: ...

class InMemoryMetricsRepository(MetricsRepository):
    def __init__(self):
        self._m: Dict[str, dict] = {}

    def ensure(self, slug: str) -> None:
        self._m.setdefault(slug, {"likes": 0, "comments": 0})

    def get(self, slug: str) -> dict:
        return self._m.setdefault(slug, {"likes": 0, "comments": 0})

    def inc_like(self, slug: str) -> None:
        self.ensure(slug)
        self._m[slug]["likes"] += 1

    def inc_comment(self, slug: str) -> None:
        self.ensure(slug)
        self._m[slug]["comments"] += 1
