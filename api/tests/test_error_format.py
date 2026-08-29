import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.errors import AppError, register_exception_handlers


class _Body(BaseModel):
    name: str


def _make_app() -> FastAPI:
    error_app = FastAPI()
    register_exception_handlers(error_app)

    @error_app.post("/_echo")
    async def echo(body: _Body):
        return body

    @error_app.get("/_boom")
    async def boom():
        raise AppError("OUT_OF_STOCK", "Товара нет в наличии", status_code=409)

    @error_app.get("/_crash")
    async def crash():
        raise RuntimeError("boom")

    return error_app


@pytest.fixture
async def error_client():
    transport = ASGITransport(app=_make_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_422_validation_error(error_client):
    response = await error_client.post("/_echo", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "name" in body["error"]["details"]["fields"]


async def test_404_not_found(error_client):
    response = await error_client.get("/_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_app_error(error_client):
    response = await error_client.get("/_boom")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OUT_OF_STOCK"


async def test_unhandled_exception(error_client):
    response = await error_client.get("/_crash")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
