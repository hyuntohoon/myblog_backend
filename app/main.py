from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
try:
    from mangum import Mangum
    _HAS_MANGUM = True
except Exception:
    _HAS_MANGUM = False

from app.routers.health import router as health_router
from app.api.routes_post import router as post_router

def create_app() -> FastAPI:
    app = FastAPI(title="myblog-api", version="0.3.0-ddd")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(post_router)
    return app

app = create_app()
handler = Mangum(app) if _HAS_MANGUM else None
