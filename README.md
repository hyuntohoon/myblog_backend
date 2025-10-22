# Blog Backend Skeleton (FastAPI)

Layered FastAPI skeleton aligned with the agreed DB schema.

**Indexes are intentionally omitted** for now—add them later after observing real query patterns.


## Structure
backend/
  app/
    api/
      routes/            # FastAPI routers (presentation layer)
      schemas.py         # Pydantic DTOs
    services/            # Use-cases (application layer)
    repositories/        # In-memory repos now; DB repos later
    models/              # Domain models (no external deps)
    db/
      schema_v0.sql      # PostgreSQL DDL (indexes intentionally omitted)
    main.py              # Entrypoint
  requirements.txt
  pyproject.toml

## Run (dev)
cd backend
uvicorn app.main:app --reload

## Endpoints
- POST /api/posts        -> create a post (in-memory)
- GET  /api/categories   -> list categories (in-memory)
- POST /api/categories   -> add category (in-memory)
- POST /api/metrics      -> batch metrics by slugs (in-memory)
