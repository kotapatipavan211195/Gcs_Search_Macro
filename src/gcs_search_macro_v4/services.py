"""Lazy production service factories; importing the API never needs ADC."""

from __future__ import annotations

from functools import lru_cache

from google.cloud import firestore, storage, tasks_v2

from gcs_search_macro_v4.jobs import FirestoreJobStore, JobStore
from gcs_search_macro_v4.queueing import CloudTasksJobQueue, JobQueue
from gcs_search_macro_v4.reports import GcsReportStore, ReportStore
from gcs_search_macro_v4.settings import get_settings


@lru_cache
def get_job_store() -> JobStore:
    settings = get_settings()
    settings.require_common_configuration()
    return FirestoreJobStore(firestore.Client(project=settings.project), settings.jobs_collection)


@lru_cache
def get_job_queue() -> JobQueue:
    settings = get_settings()
    settings.require_queue_configuration()
    return CloudTasksJobQueue(
        tasks_v2.CloudTasksClient(),
        project=settings.project,
        location=settings.tasks_location,
        queue=settings.tasks_queue,
        worker_url=settings.worker_url,
        service_account=settings.task_service_account,
    )


@lru_cache
def get_report_store() -> ReportStore:
    settings = get_settings()
    settings.require_report_configuration()
    return GcsReportStore(storage.Client(project=settings.project), settings.reports_bucket)
