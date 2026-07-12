"""API and persistence models for a hosted search job."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SearchTerm(BaseModel):
    """A user-supplied literal or safe-subset regular-expression term."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: str = Field(min_length=1)
    mode: Literal["literal", "regex"] = "literal"


class CopyRequest(BaseModel):
    """Editable destination row, validated against administrator-approved roots."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field(default="", max_length=1024)
    overwrite: bool = False

    @field_validator("prefix")
    @classmethod
    def normalise_prefix(cls, value: str) -> str:
        return value.strip().strip("/")


class BucketPath(BaseModel):
    """One editable bucket + path row from the hosted UI."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field(default="", max_length=1024)

    @field_validator("prefix")
    @classmethod
    def normalise_prefix(cls, value: str) -> str:
        value = value.strip().strip("/")
        if "\x00" in value:
            raise ValueError("Bucket path cannot contain a null character")
        return value


class CreateJobRequest(BaseModel):
    """Public request shape. Buckets are checked against the selected profile."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope_id: str = Field(min_length=1, max_length=64)
    search_type: Literal["content", "filename"] = "content"
    bucket_paths: list[BucketPath] = Field(default_factory=list, max_length=20)
    terms: list[SearchTerm] = Field(min_length=1)
    copy_request: CopyRequest | None = Field(default=None, validation_alias="copy", serialization_alias="copy")
    email_recipients: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("scope_id")
    @classmethod
    def normalise_scope_id(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("email_recipients")
    @classmethod
    def normalise_recipients(cls, values: list[str]) -> list[str]:
        return [value.strip().lower() for value in values if value.strip()]


class Requester(BaseModel):
    """Identity supplied by the IAP boundary (or dev-only local header)."""

    model_config = ConfigDict(frozen=True)

    email: str
    auth_source: Literal["iap", "development"]


class JobArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: str
    object_name: str
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    size_bytes: int | None = None


class JobRecord(BaseModel):
    """The Firestore document model. `owner_email` is the authorization key."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_email: str
    request: CreateJobRequest
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    artifact: JobArtifact | None = None
    cache_run_id: str | None = None
    files_scanned: int | None = None
    matches_found: int | None = None


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    """Owner-safe job view. Internal stack traces and bucket policy stay private."""

    job_id: str
    status: JobStatus
    scope_id: str
    search_type: Literal["content", "filename"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    files_scanned: int | None
    matches_found: int | None
    has_report: bool
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_record(cls, record: JobRecord) -> "JobResponse":
        return cls(
            job_id=record.job_id,
            status=record.status,
            scope_id=record.request.scope_id,
            search_type=record.request.search_type,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            files_scanned=record.files_scanned,
            matches_found=record.matches_found,
            has_report=record.artifact is not None,
            error_code=record.error_code,
            error_message=record.error_message,
        )


class DownloadResponse(BaseModel):
    url: str
    expires_in_seconds: int
