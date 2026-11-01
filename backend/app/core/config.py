from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Payment Orchestrator"
    environment: str = "local"
    database_url: str = (
        "postgresql+psycopg://payment_app:payment_app@localhost:5432/"
        "payment_orchestrator"
    )
    simulation_admin_token: str | None = None
    provider_callback_secret: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
