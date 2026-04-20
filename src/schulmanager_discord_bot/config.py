from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SM_", extra="ignore")

    discord_bot_token: str | None = None
    discord_api_base_url: str = "http://127.0.0.1:8000"
    discord_sync_interval_seconds: int = 120
    discord_db_path: str = "data/discord_bot.sqlite3"
    discord_guild_id: int | None = None
    discord_timezone: str = "Europe/Berlin"
    discord_category_prefix: str = "schulmanager"
    discord_digest_time: str = "07:00"
    discord_digest_enabled: bool = True
    discord_logged_in_role_id: int | None = None

    log_format: str = "text"
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
