from sqlalchemy import create_engine, text
from app.config import settings

engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
with engine.connect() as c:
    print("DB version:", c.execute(text("select version()")).scalar())
