"""Application configuration via environment variables and .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level settings, populated from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite:///./data/updates.db"

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = True
    log_level: str = "info"

    # Collector HTTP settings
    collector_timeout_seconds: int = 30
    collector_max_retries: int = 2

    # Feed pagination
    default_page_limit: int = 50
    max_page_limit: int = 200

    # ---------------------------------------------------------------------------
    # Collector source URLs
    # Override via env var using JSON-array syntax, e.g.:
    #   GEMINI_SOURCE_URLS='["https://...","https://..."]'
    # ---------------------------------------------------------------------------
    gemini_source_urls: list[str] = [
        "https://ai.google.dev/gemini-api/docs/deprecations",
        "https://ai.google.dev/gemini-api/docs/changelog",
    ]
    openai_source_urls: list[str] = [
        "https://platform.openai.com/docs/deprecations",
    ]
    anthropic_source_urls: list[str] = [
        "https://docs.anthropic.com/en/docs/about-claude/models/all-models",
        "https://docs.anthropic.com/en/release-notes/api",
    ]
    azure_source_urls: list[str] = [
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models",
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/whats-new",
    ]
    aws_source_urls: list[str] = [
        "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
        "https://docs.aws.amazon.com/bedrock/latest/userguide/doc-history.html",
    ]


settings = Settings()
