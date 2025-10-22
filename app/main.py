# app/main.py
import os
import json
from typing import List

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
import boto3

from app.api.routes import posts, categories, metrics, posts_publish
from app.db.session import get_db  # 기존 DI 유지

# --- (A) 로컬 개발 시에만 .env 로드 ---
if os.environ.get("APP_ENV", "dev") == "dev":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

# --- (B) 시크릿 로딩: Lambda면 Secrets Manager에서 1회 로드 ---
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

# 필요한 값 꺼내기(Secrets > Env 순서)
DATABASE_URL = SECRETS.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
GITHUB_TOKEN = SECRETS.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

# 프런트 도메인(정적 S3+CF)
FRONT_ORIGIN_DEFAULT = "http://localhost:4321"  # dev 기본
FRONT_ORIGIN = os.environ.get("FRONT_ORIGIN", FRONT_ORIGIN_DEFAULT)

# --- (C) 앱 생성 ---
app = FastAPI(title="Blog Backend (Lambda/Mangum)")

# 라우터 묶기(기존 유지)
app.include_router(categories.router,    prefix="/api/categories",      tags=["categories"])
app.include_router(posts.router,         prefix="/api/posts",           tags=["posts"])
app.include_router(metrics.router,       prefix="/api/metrics/batch",   tags=["metrics"])
app.include_router(posts_publish.router, prefix="/api/publish",         tags=["publish"])

# --- (D) CORS: 프런트 도메인만 허용 ---
allow_origins: List[str] = [FRONT_ORIGIN]
# dev 편의상 로컬도 허용 (원하면 제거)
if os.environ.get("APP_ENV", "dev") == "dev":
    allow_origins += ["http://localhost:4321", "http://127.0.0.1:4321"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Content-Type","Authorization"],
    max_age=600,
)

# --- (E) 헬스 & DB 핑 ---
@app.get("/health")
def health():
    return {"ok": True, "env": os.environ.get("APP_ENV", "dev")}

@app.get("/api/db/ping")
def ping(db=Depends(get_db)):
    return {"message": "✅ Database connected!"}

# --- (F) Lambda 핸들러 ---
handler = Mangum(app)