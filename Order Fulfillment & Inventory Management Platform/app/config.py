from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.example", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Order Fulfillment & Inventory Management Platform"
    database_url: str
    secret_key: str
    access_token_minutes: int 
    refresh_token_days: int 
@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
