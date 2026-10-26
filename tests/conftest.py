import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# باید قبل از import ماژول‌های app ست شود.
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/test.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("OWNER_TELEGRAM_ID", "")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import pytest
import pytest_asyncio

from app.db.base import Base
from app.db.database import engine, SessionLocal
import app.models.models  # noqa: F401


@pytest_asyncio.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield SessionLocal
    finally:
        await engine.dispose()
