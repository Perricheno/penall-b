from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.db import engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis_client import redis_pool
from app.core.request_id import RequestIDMiddleware
from app.routers import health

configure_logging(settings.APP_ENV)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    await engine.dispose()
    await redis_pool.disconnect()


app = FastAPI(title="PenAll API", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)
app.include_router(health.router)
