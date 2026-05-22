# myblog_backend

FastAPI REST API deployed as AWS Lambda via Mangum. Handles posts, categories, and metrics for the blog.

## Stack

- **Runtime**: Python 3.12, FastAPI, Mangum (Lambda adapter)
- **DB**: PostgreSQL via SQLAlchemy 2 + psycopg3
- **Config**: `pydantic-settings` — single source of truth is `app/core/config.py`
- **Deploy**: SAM (`template.yaml`, `build.sh`)

## Structure

```
app/
├── main.py          ← FastAPI app, CORS, edge_guard middleware, router registration
├── core/config.py   ← Settings (pydantic BaseSettings + Secrets Manager loader)
├── api/
│   ├── schemas.py   ← Pydantic request/response models
│   └── routes/      ← posts.py, categories.py, metrics.py
├── db/session.py    ← SQLAlchemy engine + get_db dependency
├── di.py            ← FastAPI dependency providers
├── models/          ← SQLAlchemy ORM models
├── repositories/    ← DB access layer
└── services/        ← Business logic
```

## Routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/posts` | Create post |
| GET | `/api/posts/{slug}` | Get post by slug |
| GET | `/api/categories` | List categories |
| POST | `/api/categories` | Add category |
| POST | `/api/metrics/batch` | Batch fetch likes/comments |
| GET | `/health` | Health check (public) |
| GET | `/api/db/ping` | DB connectivity check |

## Key Schema Fields

`WritePostRequest` (all optional except `title`):
- `rating: float | None` — `ge=0, le=5`
- `rating_scale: int` — default `5`, range `1–10`
- `album_classics: Dict[str, bool]` — per-album "classic" flag
- `recommended_tracks: List[RecommendedTrackInput]`

## Config

All settings live in `app/core/config.py::Settings`. No module-level `os.getenv()` anywhere.

When `SECRETS_ARN` is set, `get_settings()` overrides `DATABASE_URL` and `GITHUB_TOKEN` from AWS Secrets Manager at startup (cached via `@lru_cache`).

Required env vars:
```
DATABASE_URL=postgresql+psycopg://...
ENV=local|dev|prod
EDGE_SECRET=<shared secret with CloudFront>
FRONT_ORIGIN=https://...
SECRETS_ARN=arn:aws:secretsmanager:...   # prod only
```

## Security

Requests to `/api/*` must include `x-origin-verify: <EDGE_SECRET>` in production.  
`ENV=local` and `ENV=dev` bypass this guard entirely.

## Hard Rules

- **Never add `os.getenv()` calls in application code** — always use `settings.*`.
- **Never call `print()`** — use `logging.getLogger(__name__)`.
- **Never work directly on `main`** — branch from `main`, PR back.

## Running Locally

```bash
pip install -r requirements.txt
ENV=local uvicorn app.main:app --reload
```

## Verification

Before claiming a fix is done, run:
```bash
python -c "from app.main import app; print('import ok')"
```
