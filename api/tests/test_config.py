from app.core.config import Settings

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "test-key",
    "S3_SECRET": "test-secret",
    "S3_BUCKET": "media",
    "JWT_SECRET": "test-jwt-secret",
    "OTP_SECRET": "test-otp-secret",
    "PAY_KASPI_TRADEPOINT_ID": "000000",
    "PAY_KASPI_API_KEY": "test-kaspi-key",
    "KASPI_BASE_URL": "https://example.test",
}


def test_settings_load_from_env(monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("FCM_CREDENTIALS_JSON", raising=False)

    settings = Settings(_env_file=None)

    assert settings.APP_ENV == "dev"
    assert settings.DATABASE_URL == _REQUIRED_ENV["DATABASE_URL"]
    assert settings.SENTRY_DSN is None
    assert settings.FCM_CREDENTIALS_JSON is None
