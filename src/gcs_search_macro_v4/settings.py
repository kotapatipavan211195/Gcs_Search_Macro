"""Typed environment configuration. Secrets stay in Secret Manager / env vars."""

from __future__ import annotations

import json
import re
from functools import cached_property, lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BucketScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=63)
    prefix: str = ""


class CopyTargetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = "gcs-search"


class ScopePolicy(BaseModel):
    """Administrator-owned source configuration; never populated from HTTP input."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=3)
    buckets: list[BucketScope] = Field(min_length=1)
    dag_table: str = ""
    job_inventory_table: str = ""
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    copy_targets: dict[str, CopyTargetPolicy] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_env: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    project: str = Field(default="", validation_alias="GOOGLE_CLOUD_PROJECT")
    reports_bucket: str = Field(default="", validation_alias="GCS_SEARCH_REPORTS_BUCKET")
    jobs_collection: str = Field(default="gcs_search_jobs", validation_alias="GCS_SEARCH_JOBS_COLLECTION")
    tasks_location: str = Field(default="us-central1", validation_alias="GCS_SEARCH_TASKS_LOCATION")
    tasks_queue: str = Field(default="gcs-search-jobs", validation_alias="GCS_SEARCH_TASKS_QUEUE")
    worker_url: str = Field(default="", validation_alias="GCS_SEARCH_WORKER_URL")
    task_service_account: str = Field(default="", validation_alias="GCS_SEARCH_TASK_SERVICE_ACCOUNT")
    allowed_email_domains: str = Field(default="", validation_alias="GCS_SEARCH_ALLOWED_EMAIL_DOMAINS")
    max_terms: int = Field(default=20, ge=1, le=100, validation_alias="GCS_SEARCH_MAX_TERMS")
    max_bucket_paths: int = Field(default=20, ge=1, le=100, validation_alias="GCS_SEARCH_MAX_BUCKET_PATHS")
    max_term_length: int = Field(default=200, ge=1, le=1000, validation_alias="GCS_SEARCH_MAX_TERM_LENGTH")
    max_files_per_job: int = Field(default=100_000, ge=1, validation_alias="GCS_SEARCH_MAX_FILES_PER_JOB")
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, validation_alias="GCS_SEARCH_MAX_FILE_BYTES")
    max_result_rows: int = Field(default=100_000, ge=1, validation_alias="GCS_SEARCH_MAX_RESULT_ROWS")
    search_workers: int = Field(default=16, ge=1, le=64, validation_alias="GCS_SEARCH_SEARCH_WORKERS")
    copy_workers: int = Field(default=8, ge=1, le=32, validation_alias="GCS_SEARCH_COPY_WORKERS")
    max_inventory_rows: int = Field(default=100_000, ge=1, validation_alias="GCS_SEARCH_MAX_INVENTORY_ROWS")
    cache_dataset: str = Field(default="gcs_search_cache", validation_alias="GCS_SEARCH_CACHE_DATASET")
    cache_table_prefix: str = Field(default="", validation_alias="GCS_SEARCH_CACHE_TABLE_PREFIX")
    scope_policies_json: str = Field(default="{}", validation_alias="GCS_SEARCH_SCOPE_POLICIES_JSON")
    smtp_host: str = Field(default="", validation_alias="GCS_SEARCH_SMTP_HOST")
    smtp_port: int = Field(default=587, ge=1, le=65535, validation_alias="GCS_SEARCH_SMTP_PORT")
    smtp_user: str = Field(default="", validation_alias="GCS_SEARCH_SMTP_USER")
    smtp_password: str = Field(default="", validation_alias="GCS_SEARCH_SMTP_PASSWORD")
    smtp_from: str = Field(default="", validation_alias="GCS_SEARCH_SMTP_FROM")
    smtp_use_tls: bool = Field(default=True, validation_alias="GCS_SEARCH_SMTP_USE_TLS")

    @field_validator("cache_dataset")
    @classmethod
    def validate_cache_dataset(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", value):
            raise ValueError("GCS_SEARCH_CACHE_DATASET is not a valid BigQuery dataset name")
        return value

    @field_validator("cache_table_prefix")
    @classmethod
    def validate_cache_table_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]*", value):
            raise ValueError("GCS_SEARCH_CACHE_TABLE_PREFIX may contain only letters, digits, and underscores")
        return value

    @cached_property
    def scope_policies(self) -> dict[str, ScopePolicy]:
        try:
            raw = json.loads(self.scope_policies_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GCS_SEARCH_SCOPE_POLICIES_JSON is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("GCS_SEARCH_SCOPE_POLICIES_JSON must be a JSON object")
        try:
            return {str(scope_id).lower(): ScopePolicy.model_validate(policy) for scope_id, policy in raw.items()}
        except ValidationError as exc:
            raise RuntimeError("GCS_SEARCH_SCOPE_POLICIES_JSON has an invalid scope policy") from exc

    @cached_property
    def allowed_domains(self) -> set[str]:
        return {item.strip().lower() for item in self.allowed_email_domains.split(",") if item.strip()}

    def require_common_configuration(self) -> None:
        if self.app_env == "production":
            missing = [
                name for name, value in {
                    "GOOGLE_CLOUD_PROJECT": self.project,
                    "GCS_SEARCH_ALLOWED_EMAIL_DOMAINS": self.allowed_email_domains,
                }.items() if not value
            ]
            if missing:
                raise RuntimeError(f"Missing production configuration: {', '.join(missing)}")
        if not self.scope_policies:
            raise RuntimeError("At least one administrator-owned source scope is required")

    def require_queue_configuration(self) -> None:
        self.require_common_configuration()
        if self.app_env == "production":
            missing = [name for name, value in {
                "GCS_SEARCH_WORKER_URL": self.worker_url,
                "GCS_SEARCH_TASK_SERVICE_ACCOUNT": self.task_service_account,
            }.items() if not value]
            if missing:
                raise RuntimeError(f"Missing production configuration: {', '.join(missing)}")

    def require_report_configuration(self) -> None:
        self.require_common_configuration()
        if self.app_env == "production" and not self.reports_bucket:
            raise RuntimeError("Missing production configuration: GCS_SEARCH_REPORTS_BUCKET")

    def require_runtime_configuration(self) -> None:
        """Backward-compatible full validation for explicit startup checks."""
        self.require_queue_configuration()
        self.require_report_configuration()


@lru_cache
def get_settings() -> Settings:
    return Settings()
