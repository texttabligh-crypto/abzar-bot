from app.db.base import Base
from app.db.database import engine
from app.db.migrate import run_safe_migrations
import app.models.models  # noqa: F401

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_safe_migrations()
