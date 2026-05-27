# myblog_backend

FastAPI REST API deployed as AWS Lambda via Mangum. Handles posts, categories, metrics, and MDX publishing for the blog. (Absorbed `myblog_publish` in ARCH-11, 2026-05-27.)

## Stack

- **Runtime**: Python 3.12, FastAPI, Mangum (Lambda adapter)
- **DB**: PostgreSQL via SQLAlchemy 2 + psycopg3; ORM models from `myblog-shared-db` package (`myblog_shared_db.models`)
- **Config**: `pydantic-settings` — single source of truth is `app/core/config.py`
- **Deploy**: CI via `aws lambda update-function-code` on push to `main`. Terraform owns Lambda config (role, runtime, env vars).

## Structure

```
app/
├── main.py          ← FastAPI app, CORS, edge_guard middleware, router registration
├── core/
│   ├── config.py    ← Settings (pydantic BaseSettings + Secrets Manager loader)
│   └── auth.py      ← Cognito JWT validation (require_cognito_token dependency)
├── api/
│   ├── schemas.py   ← Pydantic request/response models
│   └── routes/      ← posts.py, categories.py, metrics.py, publish.py
├── db/session.py    ← SQLAlchemy engine + get_db dependency
├── di.py            ← FastAPI dependency providers
├── repositories/    ← DB access layer
└── services/        ← Business logic (incl. publish_service.py)
```

## Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/posts` | — | Create post |
| GET | `/api/posts/{slug}` | — | Get post by slug |
| GET | `/api/categories` | — | List categories |
| POST | `/api/categories` | — | Add category |
| POST | `/api/metrics/batch` | — | Batch fetch likes/comments |
| POST | `/api/publish` | Cognito JWT | Write MDX file to myblog_front via GitHub API |
| GET | `/health` | — | Health check (public) |
| GET | `/api/db/ping` | — | DB connectivity check |

`POST /api/publish` is protected by the API Gateway Cognito JWT authorizer (not edge_guard).

## Key Schema Fields

`WritePostRequest` (all optional except `title`):
- `rating: float | None` — `ge=0, le=5`
- `rating_scale: int` — default `5`, range `1–10`
- `album_classics: Dict[str, bool]` — per-album "classic" flag
- `recommended_tracks: List[RecommendedTrackInput]`

`CreatePostReq` (publish route):
- `title`, `body_mdx`, `posted_date`, `status`, `category`, `slug` (optional — auto-generated from title), `rating`, `album_ids`, `artist_ids`

## Config

All settings live in `app/core/config.py::Settings`. No module-level `os.getenv()` anywhere.

When `SECRETS_ARN` is set, `get_settings()` overrides `DATABASE_URL`, `EDGE_SECRET`, and `GITHUB_TOKEN` from AWS Secrets Manager at startup (cached via `@lru_cache`).

Required env vars (prod Lambda):
```
ENV=prod
APP_ENV=prod
SECRETS_ARN=arn:aws:secretsmanager:ap-northeast-2:...:myblog/backend-...
GITHUB_REPO_OWNER=hyuntohoon
GITHUB_REPO_NAME=myblog_front
GITHUB_REPO_BRANCH=main
CONTENT_DIR=content/blog
```

`GITHUB_TOKEN`, `DATABASE_URL`, `EDGE_SECRET` come from `myblog/backend` Secrets Manager (JSON).

## Security

**Two entry points, two auth mechanisms:**

- **CloudFront → Lambda** (most routes): `edge_guard` middleware checks `x-origin-verify: <EDGE_SECRET>`. Bypassed entirely for `ENV=local` or `ENV=dev`.
- **API Gateway → Lambda** (`POST /api/publish`): Cognito JWT validated at API Gateway ingress. `edge_guard` passes Bearer-token requests through without checking `x-origin-verify`.

**Hard rules:**
- Never log `GITHUB_TOKEN` or any secret value.
- Never add `os.getenv()` in application code — always use `settings.*`.
- Never call `print()` — use `logging.getLogger(__name__)`.
- Never work directly on `main` — branch from `main`, PR back.

## Running Locally

```bash
pip install -r requirements.txt
ENV=local uvicorn app.main:app --reload
```

## Verification

```bash
python -c "from app.main import app; print('import ok')"
pytest tests/ -v
```
