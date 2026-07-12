"""Cloud Tasks-only worker entry point. End users must never invoke it."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status

from gcs_search_macro_v4.emailer import send_report
from gcs_search_macro_v4.executor import ProductionExecutor
from gcs_search_macro_v4.jobs import JobStore
from gcs_search_macro_v4.models import JobStatus
from gcs_search_macro_v4.policy import validate_request
from gcs_search_macro_v4.reports import ReportStore
from gcs_search_macro_v4.services import get_job_store, get_report_store
from gcs_search_macro_v4.settings import get_settings


LOGGER = logging.getLogger(__name__)


def create_worker_app() -> FastAPI:
    app = FastAPI(title="GCS Search Macro worker", docs_url=None, redoc_url=None)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def run_job(
        job_id: str,
        request: Request,
        store: JobStore = Depends(get_job_store),
        reports: ReportStore = Depends(get_report_store),
    ) -> Response:
        settings = get_settings()
        if settings.app_env == "production" and not request.headers.get("X-CloudTasks-TaskName"):
            # Defense in depth: Cloud Run IAM allows only the task dispatcher;
            # this header makes an accidental broader IAM grant non-functional.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cloud Tasks invocation required")

        record = store.get(job_id)
        if record is None or record.status is not JobStatus.QUEUED:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        try:
            policy = validate_request(record.request, settings)
        except HTTPException:
            store.fail(job_id, error_code="POLICY_REJECTED", error_message="Job policy is no longer valid")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        try:
            claimed = store.claim(job_id)
            if claimed is None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            with tempfile.TemporaryDirectory(prefix=f"gcs-search-{job_id}-") as temporary_dir:
                outcome = ProductionExecutor().execute(claimed, policy, settings, Path(temporary_dir))
                artifact = reports.upload(claimed.owner_email, claimed.job_id, outcome.report_path)
                if claimed.request.email_recipients:
                    try:
                        send_report(
                            settings,
                            recipients=claimed.request.email_recipients,
                            report_path=outcome.report_path,
                            job_id=claimed.job_id,
                        )
                    except Exception:
                        # The report is safely available for download even
                        # when a non-essential email relay is unavailable.
                        LOGGER.exception("Email delivery failed for job %s", claimed.job_id)
                store.succeed(
                    claimed.job_id,
                    artifact,
                    cache_run_id=outcome.cache_run_id,
                    files_scanned=outcome.files_scanned,
                    matches_found=outcome.matches_found,
                )
        except Exception as exc:
            # Keep task retries from blindly duplicating copy/cache side
            # effects. Failed jobs are visible to the owner and can be rerun.
            LOGGER.exception("Search execution failed for job %s", job_id)
            store.fail(
                job_id,
                error_code="EXECUTION_FAILED",
                error_message=f"Search execution failed. Contact support with job ID {job_id}.",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_worker_app()
