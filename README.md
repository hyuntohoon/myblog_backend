# FastAPI + DDD Skeleton (Updated Schema)

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Run Postgres
```bash
docker rm -f myblog-db 2>/dev/null || true
docker run --name myblog-db   -e POSTGRES_USER=myblog_user   -e POSTGRES_PASSWORD=myblog_pass   -e POSTGRES_DB=myblog   -p 5433:5432 -d postgres:16

# Wait until ready then:
docker exec -i myblog-db psql -U myblog_user -d myblog < scripts/init_db.sql
```

### Start API
```bash
uvicorn app.main:create_app --factory --reload
# GET http://localhost:8000/health/live
# POST http://localhost:8000/posts
```
