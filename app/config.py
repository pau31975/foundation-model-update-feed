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
        "https://ai.google.dev/gemini-api/docs/models",
        "https://ai.google.dev/gemini-api/docs/changelog",
    ]
    openai_source_urls: list[str] = [
        # Canonical URL (platform.openai.com/docs/deprecations redirects here)
        "https://developers.openai.com/api/docs/deprecations",
        "https://developers.openai.com/api/docs/models",
        "https://developers.openai.com/api/docs/changelog",
    ]
    anthropic_source_urls: list[str] = [
        # Canonical URLs (docs.anthropic.com redirects here)
        "https://platform.claude.com/docs/en/about-claude/models/all-models",
        "https://platform.claude.com/docs/en/release-notes/api",
        "https://platform.claude.com/docs/en/about-claude/model-deprecations",
    ]
    azure_source_urls: list[str] = [
        "https://learn.microsoft.com/en-us/azure/foundry-classic/openai/whats-new",
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models",
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/whats-new",
    ]
    aws_source_urls: list[str] = [
        "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
        "https://docs.aws.amazon.com/bedrock/latest/userguide/doc-history.html",
        "https://docs.aws.amazon.com/bedrock/latest/userguide/release-notes.html",
    ]

    # ---------------------------------------------------------------------------
    # RSS feed URLs for automatic detection of new model announcements.
    # Anthropic and Azure do not expose public RSS feeds.
    # ---------------------------------------------------------------------------
    openai_rss_url: str = "https://openai.com/blog/rss.xml"
    google_rss_url: str = "https://blog.google/products/gemini/rss/"
    aws_rss_url: str = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"


settings = Settings()
