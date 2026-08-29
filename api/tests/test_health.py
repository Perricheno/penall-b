async def test_health_ok(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}
