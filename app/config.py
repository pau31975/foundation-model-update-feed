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
    database_url: str

    # Server
    host: str
    port: int
    reload: bool
    log_level: str

    # Collector HTTP settings
    collector_timeout_seconds: int
    collector_max_retries: int

    # Feed pagination
    default_page_limit: int
    max_page_limit: int

    # ---------------------------------------------------------------------------
    # Collector source URLs — set via JSON-array syntax in .env, e.g.:
    #   GEMINI_SOURCE_URLS='["https://...","https://..."]'
    # ---------------------------------------------------------------------------
    gemini_source_urls: list[str]
    openai_source_urls: list[str]
    anthropic_source_urls: list[str]
    azure_source_urls: list[str]
    aws_source_urls: list[str]

    # ---------------------------------------------------------------------------
    # RSS feed URLs — Anthropic and Azure do not expose public RSS feeds.
    # ---------------------------------------------------------------------------
    openai_rss_url: str
    google_rss_url: str
    aws_rss_url: str


settings = Settings()
