# app/main.py
import os
import json
from typing import List

from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
import boto3

# -----------------------------
# 환경설정
# -----------------------------
APP_ENV = os.environ.get("APP_ENV", "dev")

# dev면 .env 로드
if APP_ENV == "dev":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

# -----------------------------
# 시크릿/환경값 로드
# -----------------------------
SECRETS_ARN = os.environ.get("SECRETS_ARN", "")
SECRETS_CACHE = {}

def _load_secrets_once() -> dict:
    global SECRETS_CACHE
    if SECRETS_CACHE:
        return SECRETS_CACHE
    if not SECRETS_ARN:
        return {}
    sm = boto3.client("secretsmanager")
    val = sm.get_secret_value(SecretId=SECRETS_ARN)
    SECRETS_CACHE = json.loads(val["SecretString"])
    return SECRETS_CACHE

SECRETS = _load_secrets_once()

# 필요한 값 꺼내기(Secrets > Env)
DATABASE_URL = SECRETS.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
GITHUB_TOKEN  = SECRETS.get("GITHUB_TOKEN")  or os.environ.get("GITHUB_TOKEN")

# 프런트 도메인
FRONT_ORIGIN_DEFAULT = "http://localhost:4321"
FRONT_ORIGIN = os.environ.get("FRONT_ORIGIN", FRONT_ORIGIN_DEFAULT)

# 보호/예외 경로 & 헤더
EDGE_SECRET = os.environ.get("EDGE_SECRET", "")
ALLOW_PUBLIC_HEALTH = os.environ.get("ALLOW_PUBLIC_HEALTH", "true").lower() == "true"
PROTECTED_PREFIXES = ("/api",)
PUBLIC_PATHS = {"/health"}

# -----------------------------
# 앱 생성 & 미들웨어
# -----------------------------
app = FastAPI(title="Blog Backend (Lambda/Mangum)", debug=(APP_ENV == "dev"))

# CORS
allow_origins: List[str] = [FRONT_ORIGIN]
if APP_ENV == "dev":
    allow_origins += ["http://localhost:4321", "http://127.0.0.1:4321"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Content-Type","Authorization","x-origin-verify"],
    max_age=600,
)

# 단일 미들웨어만 유지 (중복 금지!)
@app.middleware("http")
async def edge_guard(request: Request, call_next):
    path = request.url.path

    # ✅ dev에서는 우회
    if APP_ENV == "dev":
        return await call_next(request)

    if request.method == "OPTIONS":
        return await call_next(request)

    if ALLOW_PUBLIC_HEALTH and path in PUBLIC_PATHS:
        return await call_next(request)

    if path.startswith(PROTECTED_PREFIXES):
        if request.headers.get("x-origin-verify") != EDGE_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    return await call_next(request)

# -----------------------------
# 라우터
# -----------------------------
from app.api.routes import posts, categories, metrics, posts_publish  # (또는 상대임포트로 변경 가능)
from app.db.session import get_db

app.include_router(categories.router,    prefix="/api/categories",      tags=["categories"])
app.include_router(posts.router,         prefix="/api/posts",           tags=["posts"])
app.include_router(metrics.router,       prefix="/api/metrics/batch",   tags=["metrics"])
app.include_router(posts_publish.router, prefix="/api/publish",         tags=["publish"])

# -----------------------------
# 기본 라우트
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "env": APP_ENV}

@app.get("/api/db/ping")
def ping(db=Depends(get_db)):
    return {"message": "✅ Database connected!"}

# -----------------------------
# Lambda 핸들러
# -----------------------------
handler = Mangum(app)