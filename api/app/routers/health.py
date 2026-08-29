from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.redis_client import get_redis

router = APIRouter(tags=["health"])
logger = structlog.get_logger(__name__)


@router.get("/health")
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> JSONResponse:
    checks: dict[str, str] = {}
    healthy = True

    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        logger.warning("health_check_failed", service="postgres", exc_info=True)
        checks["postgres"] = "error"
        healthy = False

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        logger.warning("health_check_failed", service="redis", exc_info=True)
        checks["redis"] = "error"
        healthy = False

    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        {"status": "ok" if healthy else "error", "checks": checks}, status_code=code
    )
