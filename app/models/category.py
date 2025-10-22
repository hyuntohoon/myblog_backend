from dataclasses import dataclass
from datetime import datetime

@dataclass
class Category:
    id: int
    name: str
    slug: str
    created_at: datetime