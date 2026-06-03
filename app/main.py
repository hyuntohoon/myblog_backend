# app/main.py
from typing import List

from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from app.core.config import settings

APP_ENV = settings.ENV

# -----------------------------
# App + middleware
# -----------------------------
app = FastAPI(title="Blog Backend (Lambda/Mangum)", debug=(APP_ENV in ("dev", "local")))

allow_origins: List[str] = [settings.FRONT_ORIGIN]
if APP_ENV in ("dev", "local"):
    allow_origins += ["http://localhost:4321", "http://127.0.0.1:4321"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "x-origin-verify"],
    max_age=600,
)

PUBLIC_PATHS = {"/health"}
PROTECTED_PREFIXES = ("/api",)


@app.middleware("http")
async def edge_guard(request: Request, call_next):
    if APP_ENV in ("dev", "local"):
        return await call_next(request)

    if request.method == "OPTIONS":
        return await call_next(request)

    if settings.ALLOW_PUBLIC_HEALTH and request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    if request.url.path.startswith(PROTECTED_PREFIXES):
        # API Gateway validates JWT before invoking Lambda; those requests carry
        # Authorization: Bearer but no x-origin-verify (CloudFront adds the latter).
        # Trust either: a matching edge secret (CloudFront path) or a Bearer token
        # (API Gateway path — Cognito JWT already validated at ingress).
        has_bearer = request.headers.get("authorization", "").startswith("Bearer ")
        if not has_bearer and request.headers.get("x-origin-verify") != settings.EDGE_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    return await call_next(request)


# -----------------------------
# Routers
# -----------------------------
from app.api.routes import posts, categories, metrics
from app.api.routes import publish
from app.api.routes import buckets
from app.db.session import get_db

app.include_router(categories.router, prefix="/api/categories",    tags=["categories"])
app.include_router(posts.router,      prefix="/api/posts",         tags=["posts"])
app.include_router(metrics.router,    prefix="/api/metrics/batch", tags=["metrics"])
app.include_router(publish.router,    prefix="/api/publish",       tags=["publish"])
app.include_router(buckets.router,    prefix="/api/buckets",       tags=["buckets"])


# -----------------------------
# Base routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "env": APP_ENV}


@app.get("/api/db/ping")
def ping(db=Depends(get_db)):
    return {"message": "Database connected"}


# -----------------------------
# Lambda handler
# -----------------------------
handler = Mangum(app)
