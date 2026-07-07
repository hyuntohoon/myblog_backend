# app/main.py
from typing import List

from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from app.core.config import settings
from app.core.auth import verify_token

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
        # Trusted CloudFront edge: every front request (incl. the public metrics
        # beacon and section list) reaches the backend through CloudFront, which
        # injects x-origin-verify == EDGE_SECRET. Trust that and pass through.
        # FIX-bug-audit-2026-07 WS-A: require EDGE_SECRET truthy first — if the
        # SSM/secrets load failed, EDGE_SECRET is "" and a request carrying an
        # empty (or absent-but-present) x-origin-verify header would compare equal
        # and fail OPEN. Cognito misconfig already fails closed (503); match it.
        if settings.EDGE_SECRET and request.headers.get("x-origin-verify") == settings.EDGE_SECRET:
            return await call_next(request)

        # No edge secret => a direct (raw invoke domain) request. It must carry a
        # REAL Cognito JWT. STAB-2 / AUTH-3: the old guard trusted the literal
        # "Bearer " prefix, so a garbage `Bearer x` bypassed it on every
        # authorizer-less route. Validate the token instead of trusting the prefix.
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                verify_token(auth_header[len("Bearer "):])
                return await call_next(request)
            except HTTPException as exc:
                # JWKS outage (503) stays 503; a bad/expired/forged token => 403
                # (clean reject — the old reject path raised inside middleware and
                # surfaced as a 500, STAB-2 / P8-7).
                code = exc.status_code if exc.status_code == 503 else 403
                return JSONResponse(status_code=code, content={"detail": exc.detail})

        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    return await call_next(request)


# -----------------------------
# Routers
# -----------------------------
from app.api.routes import posts, sections, metrics
from app.api.routes import tags
from app.api.routes import publish
from app.api.routes import buckets
from app.api.routes import library
from app.api.routes import research
from app.api.routes import genres
from app.api.routes import playback
from app.api.routes import lyrics
from app.api.routes import me
from app.api.routes import reviews
from app.api.routes import members
from app.db.session import get_db

app.include_router(sections.router,   prefix="/api/sections",      tags=["sections"])
app.include_router(tags.router,       prefix="/api/tags",          tags=["tags"])
app.include_router(posts.router,      prefix="/api/posts",         tags=["posts"])
app.include_router(metrics.router,    prefix="/api/metrics/batch", tags=["metrics"])
app.include_router(publish.router,    prefix="/api/publish",       tags=["publish"])
app.include_router(buckets.router,    prefix="/api/buckets",       tags=["buckets"])
app.include_router(library.router,    prefix="/api/library",       tags=["library"])
app.include_router(research.router,   prefix="/api/research",      tags=["research"])
app.include_router(genres.router,     prefix="/api/genres",        tags=["genres"])
app.include_router(playback.router,   prefix="/api/playback",      tags=["playback"])
app.include_router(lyrics.router,     prefix="/api/lyrics",        tags=["lyrics"])
app.include_router(me.router,         prefix="/api/me",            tags=["me"])
app.include_router(reviews.router,    prefix="/api/reviews",       tags=["reviews"])
app.include_router(members.router,    prefix="/api/members",       tags=["members"])


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
