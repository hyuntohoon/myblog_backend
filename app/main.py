from fastapi import FastAPI, Depends
from app.api.routes import posts, categories, metrics
from app.db.session import get_db
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import posts_publish
from dotenv import load_dotenv
load_dotenv()

app = FastAPI(title="Blog Backend (Skeleton)")

app.include_router(categories.router, prefix="/api/categories", tags=["categories"])
app.include_router(posts.router,      prefix="/api/posts",      tags=["posts"])
app.include_router(metrics.router, prefix="/api/metrics/batch", tags=["metrics"])
app.include_router(posts_publish.router, prefix="/api/publish",      tags=["publish"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4321", "http://127.0.0.1:4321", "http://ratemymusic.blog"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/db/ping")
def ping(db=Depends(get_db)):
    return {"message": "✅ Database connected!"}