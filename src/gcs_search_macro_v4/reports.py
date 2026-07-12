"""Private, owner-namespaced report storage and short-lived download URLs."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from google.auth import credentials as auth_credentials
from google.auth.transport.requests import Request as AuthRequest
from google.cloud import storage

from gcs_search_macro_v4.models import JobArtifact

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def report_object_name(owner_email: str, job_id: str) -> str:
    """No user-controlled filename reaches object storage."""
    owner_hash = hashlib.sha256(owner_email.lower().encode()).hexdigest()[:20]
    return f"reports/{owner_hash}/{job_id}/gcs-search-results.xlsx"


class ReportStore(Protocol):
    def upload(self, owner_email: str, job_id: str, local_path: Path) -> JobArtifact: ...
    def signed_download_url(self, artifact: JobArtifact, expires_in_seconds: int) -> str: ...


class GcsReportStore:
    def __init__(self, client: storage.Client, bucket_name: str) -> None:
        self._credentials = client._credentials
        self._bucket = client.bucket(bucket_name)

    def upload(self, owner_email: str, job_id: str, local_path: Path) -> JobArtifact:
        object_name = report_object_name(owner_email, job_id)
        blob = self._bucket.blob(object_name)
        blob.upload_from_filename(str(local_path), content_type=XLSX_CONTENT_TYPE)
        return JobArtifact(bucket=self._bucket.name, object_name=object_name, size_bytes=local_path.stat().st_size)

    def signed_download_url(self, artifact: JobArtifact, expires_in_seconds: int) -> str:
        blob = self._bucket.blob(artifact.object_name)
        arguments = {
            "version": "v4",
            "expiration": timedelta(seconds=expires_in_seconds),
            "method": "GET",
            "response_disposition": "attachment; filename=gcs-search-results.xlsx",
        }
        if not isinstance(self._credentials, auth_credentials.Signing):
            # Cloud Run's metadata credentials have no local private key.
            # Supplying the refreshed token and service-account identity makes
            # the storage library use IAM signBlob instead.
            self._credentials.refresh(AuthRequest())
            service_account_email = getattr(self._credentials, "service_account_email", "")
            if not service_account_email:
                raise RuntimeError("Report signing service account identity is unavailable")
            arguments.update({
                "service_account_email": service_account_email,
                "access_token": self._credentials.token,
            })
        return blob.generate_signed_url(
            **arguments,
        )


class LocalReportStore:
    """Development/test report store. Do not deploy this implementation."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def upload(self, owner_email: str, job_id: str, local_path: Path) -> JobArtifact:
        object_name = report_object_name(owner_email, job_id)
        destination = self._root / object_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(local_path.read_bytes())
        return JobArtifact(bucket="local", object_name=object_name, size_bytes=destination.stat().st_size)

    def signed_download_url(self, artifact: JobArtifact, expires_in_seconds: int) -> str:
        return f"file://{(self._root / artifact.object_name).resolve()}"
