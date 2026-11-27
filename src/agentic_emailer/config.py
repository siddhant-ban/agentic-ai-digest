"""Application settings loaded from environment variables, .env, and local secret fallbacks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(path: Path, base_dir: Path | None = None) -> str | None:
    """Read a trimmed secret from a local file path if it exists."""
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        if base_dir is not None:
            candidates.append(base_dir / path)
        candidates.append(Path.cwd() / path)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
    return None


class Settings(BaseSettings):
    """Typed runtime configuration for Google ADK, budgets, and SMTP delivery."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # GenAI backend toggle (Vertex AI in GCP vs. Gemini API locally)
    google_genai_use_vertexai: bool = Field(
        default=False,
        validation_alias=AliasChoices("GOOGLE_GENAI_USE_VERTEXAI", "google_genai_use_vertexai"),
    )
    gcp_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "gcp_project_id"),
    )
    gcp_location: str = Field(
        default="us-central1",
        validation_alias=AliasChoices("GCP_LOCATION", "GOOGLE_CLOUD_LOCATION", "gcp_location"),
    )
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY", "gemini_api_key"),
    )
    gemini_api_key_file: Path = Path("gemini_api_key.txt")

    # Model selection
    research_model: str = "gemini-3.1-flash-lite"
    synthesis_model: str = "gemini-3.1-flash-lite"
    critic_model: str = "gemini-3.1-flash-lite"
    eval_judge_model: str | None = None
    embedding_model: str = "gemini-embedding-001"

    # Hard per-run budget ceilings
    max_llm_calls_per_run: int = Field(default=60, ge=1)
    max_total_tokens_per_run: int = Field(default=350_000, ge=1_000)
    max_article_scrapes_per_run: int = Field(default=20, ge=0)
    max_search_calls_per_run: int = Field(default=20, ge=0)
    max_critic_revisions: int = Field(default=2, ge=0, le=5)

    # Cross-day memory & semantic clustering settings
    memory_backend: Literal["local", "firestore"] = "local"
    local_memory_dir: Path = Path(".local_memory")
    firestore_database: str = "(default)"
    seen_url_window_days: int = Field(default=14, ge=1, le=365)
    storyline_window_days: int = Field(default=7, ge=1, le=365)
    memory_retention_days: int = Field(default=30, ge=1, le=3650)
    cluster_merge_threshold: float = Field(default=0.84, ge=0.0, le=1.0)
    storyline_link_threshold: float = Field(default=0.76, ge=0.0, le=1.0)
    semantic_dedup_threshold: float = Field(default=0.94, ge=0.0, le=1.0)

    # Email / SMTP settings
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_sender: str = ""
    email_password: str | None = None
    smtp_password_file: Path = Path("smtp_password.txt")

    # Observability & structured logging settings
    otel_cloud_trace_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OTEL_CLOUD_TRACE_ENABLED", "otel_cloud_trace_enabled"),
    )
    log_format: Literal["text", "json"] = Field(
        default="text",
        validation_alias=AliasChoices("LOG_FORMAT", "log_format"),
    )

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _validate_clustering_thresholds(self) -> Settings:
        if self.storyline_link_threshold > self.semantic_dedup_threshold:
            raise ValueError(
                "storyline_link_threshold must be <= semantic_dedup_threshold "
                f"({self.storyline_link_threshold} > {self.semantic_dedup_threshold})"
            )
        min_retention = max(self.seen_url_window_days, self.storyline_window_days)
        if self.memory_retention_days < min_retention:
            raise ValueError(
                "memory_retention_days must be >= max(seen_url_window_days, "
                f"storyline_window_days) ({self.memory_retention_days} < {min_retention})"
            )
        return self

    def resolve_gemini_api_key(self, base_dir: Path | None = None) -> str | None:
        """Return Gemini API key from env/settings or local fallback file."""
        if self.gemini_api_key and self.gemini_api_key.strip():
            return self.gemini_api_key.strip()
        return _read_secret_file(self.gemini_api_key_file, base_dir=base_dir)

    def resolve_email_password(self, base_dir: Path | None = None) -> str | None:
        """Return SMTP password from env/settings or local fallback file."""
        if self.email_password and self.email_password.strip():
            return self.email_password.strip()
        return _read_secret_file(self.smtp_password_file, base_dir=base_dir)

    def apply_genai_env(self, base_dir: Path | None = None) -> None:
        """Configure environment variables expected by google-adk and google-genai."""
        if self.google_genai_use_vertexai:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
            if self.gcp_project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = self.gcp_project_id
            os.environ["GOOGLE_CLOUD_LOCATION"] = self.gcp_location
            # Avoid conflicting API key auth when explicitly targeting Vertex AI IAM
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)
            return

        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
        api_key = self.resolve_gemini_api_key(base_dir=base_dir)
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key
            os.environ.pop("GEMINI_API_KEY", None)
