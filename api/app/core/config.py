from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения из ENV/.env. Секреты — только отсюда, см. CLAUDE.md п.9."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_ENV: str = "dev"

    DATABASE_URL: str
    REDIS_URL: str

    S3_ENDPOINT: str
    S3_KEY: str
    S3_SECRET: str
    S3_BUCKET: str

    JWT_SECRET: str
    OTP_SECRET: str

    PAY_KASPI_TRADEPOINT_ID: str
    PAY_KASPI_API_KEY: str
    KASPI_BASE_URL: str

    FCM_CREDENTIALS_JSON: str | None = None
    SENTRY_DSN: str | None = None


settings = Settings()
