from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file="config/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    gateway_host: str = Field(
        default="127.0.0.1",
        alias="GATEWAY_HOST",
    )
    gateway_port: int = Field(
        default=8000,
        alias="GATEWAY_PORT",
    )
    models_yaml_path: Path = Field(
        default=Path("config/models.yaml"),
        alias="MODELS_YAML_PATH",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        alias="OLLAMA_BASE_URL",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance for dependency injection."""
    return Settings()
