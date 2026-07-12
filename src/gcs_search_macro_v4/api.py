"""IAP-facing API: create, inspect, cancel, and download owned search jobs."""

from __future__ import annotations

import importlib.resources as resources

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse

from gcs_search_macro_v4.auth import get_requester
from gcs_search_macro_v4.jobs import JobStore
from gcs_search_macro_v4.models import (
    CreateJobRequest,
    DownloadResponse,
    JobCreatedResponse,
    JobRecord,
    JobResponse,
    JobStatus,
    Requester,
)
from gcs_search_macro_v4.policy import validate_request
from gcs_search_macro_v4.queueing import JobQueue
from gcs_search_macro_v4.reports import ReportStore
from gcs_search_macro_v4.services import get_job_queue, get_job_store, get_report_store
from gcs_search_macro_v4.settings import get_settings

DOWNLOAD_URL_TTL_SECONDS = 15 * 60


def _owner_record(store: JobStore, job_id: str, requester: Requester) -> JobRecord:
    record = store.get(job_id)
    # Return 404 for another owner's job to avoid job-ID enumeration.
    if record is None or record.owner_email != requester.email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return record


def create_app() -> FastAPI:
    app = FastAPI(title="GCS Search Macro", version="4.1.0", docs_url=None, redoc_url=None)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = resources.files("gcs_search_macro_v4").joinpath("static", "index.html").read_text()
        return HTMLResponse(page)

    @app.get("/v1/me")
    def me(requester: Requester = Depends(get_requester)) -> dict[str, str]:
        return {"email": requester.email}

    @app.post("/v1/jobs", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
    def create_job(
        request: CreateJobRequest,
        requester: Requester = Depends(get_requester),
        store: JobStore = Depends(get_job_store),
        queue: JobQueue = Depends(get_job_queue),
    ) -> JobCreatedResponse:
        settings = get_settings()
        validate_request(request, settings)
        record = JobRecord(owner_email=requester.email, request=request)
        store.create(record)
        try:
            queue.enqueue(record.job_id)
        except Exception as exc:
            # The request cannot be lost silently: surface a terminal state
            # instead of leaving an unqueued job that looks pending forever.
            store.fail(record.job_id, error_code="QUEUE_UNAVAILABLE", error_message=str(exc))
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job queue is unavailable") from exc
        return JobCreatedResponse(job_id=record.job_id, status=record.status)

    @app.get("/v1/scopes")
    def list_scopes(requester: Requester = Depends(get_requester)) -> dict[str, list[dict[str, object]]]:
        """Expose approved bucket roots for the selected access profile only."""
        settings = get_settings()
        return {
            "scopes": [
                {
                    "scope_id": scope_id,
                    "buckets": [
                        {"bucket": bucket.name, "prefix": bucket.prefix}
                        for bucket in policy.buckets
                    ],
                    "copy_targets": [
                        {"bucket": target.bucket, "prefix": target.prefix}
                        for _, target in sorted(policy.copy_targets.items())
                    ],
                }
                for scope_id, policy in sorted(settings.scope_policies.items())
            ]
        }

    @app.get("/v1/jobs/{job_id}", response_model=JobResponse)
    def get_job(
        job_id: str,
        requester: Requester = Depends(get_requester),
        store: JobStore = Depends(get_job_store),
    ) -> JobResponse:
        return JobResponse.from_record(_owner_record(store, job_id, requester))

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(
        job_id: str,
        requester: Requester = Depends(get_requester),
        store: JobStore = Depends(get_job_store),
    ) -> JobResponse:
        record = _owner_record(store, job_id, requester)
        cancelled = store.cancel_if_queued(job_id)
        if cancelled is None:
            if record.status is JobStatus.RUNNING:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A running job cannot be cancelled")
            return JobResponse.from_record(record)
        return JobResponse.from_record(cancelled)

    @app.get("/v1/jobs/{job_id}/download", response_model=DownloadResponse)
    def download_job(
        job_id: str,
        requester: Requester = Depends(get_requester),
        store: JobStore = Depends(get_job_store),
        reports: ReportStore = Depends(get_report_store),
    ) -> DownloadResponse:
        record = _owner_record(store, job_id, requester)
        if record.status is not JobStatus.SUCCEEDED or record.artifact is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Report is not available")
        return DownloadResponse(
            url=reports.signed_download_url(record.artifact, DOWNLOAD_URL_TTL_SECONDS),
            expires_in_seconds=DOWNLOAD_URL_TTL_SECONDS,
        )

    return app


app = create_app()
