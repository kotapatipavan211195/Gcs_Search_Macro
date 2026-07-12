"""Durable job state with owner checks kept outside the worker payload."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from google.cloud import firestore

from gcs_search_macro_v4.models import JobArtifact, JobRecord, JobStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStore(Protocol):
    def create(self, record: JobRecord) -> None: ...
    def get(self, job_id: str) -> JobRecord | None: ...
    def claim(self, job_id: str) -> JobRecord | None: ...
    def succeed(self, job_id: str, artifact: JobArtifact, *, cache_run_id: str | None, files_scanned: int | None, matches_found: int) -> JobRecord: ...
    def fail(self, job_id: str, *, error_code: str, error_message: str) -> JobRecord: ...
    def cancel_if_queued(self, job_id: str) -> JobRecord | None: ...


class InMemoryJobStore:
    """Development/test implementation; production always uses Firestore."""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}

    def create(self, record: JobRecord) -> None:
        if record.job_id in self._records:
            raise ValueError("Job already exists")
        self._records[record.job_id] = record.model_copy(deep=True)

    def get(self, job_id: str) -> JobRecord | None:
        record = self._records.get(job_id)
        return record.model_copy(deep=True) if record else None

    def claim(self, job_id: str) -> JobRecord | None:
        record = self._records.get(job_id)
        if record is None or record.status is not JobStatus.QUEUED:
            return None
        record.status = JobStatus.RUNNING
        record.started_at = _now()
        return record.model_copy(deep=True)

    def succeed(self, job_id: str, artifact: JobArtifact, *, cache_run_id: str | None, files_scanned: int | None, matches_found: int) -> JobRecord:
        record = self._require_running(job_id)
        record.status = JobStatus.SUCCEEDED
        record.finished_at = _now()
        record.artifact = artifact
        record.cache_run_id = cache_run_id
        record.files_scanned = files_scanned
        record.matches_found = matches_found
        return record.model_copy(deep=True)

    def fail(self, job_id: str, *, error_code: str, error_message: str) -> JobRecord:
        record = self._require_active(job_id)
        record.status = JobStatus.FAILED
        record.finished_at = _now()
        record.error_code = error_code
        record.error_message = error_message[:1000]
        return record.model_copy(deep=True)

    def cancel_if_queued(self, job_id: str) -> JobRecord | None:
        record = self._records.get(job_id)
        if record is None or record.status is not JobStatus.QUEUED:
            return None
        record.status = JobStatus.CANCELLED
        record.finished_at = _now()
        return record.model_copy(deep=True)

    def _require_active(self, job_id: str) -> JobRecord:
        record = self._records.get(job_id)
        if record is None or record.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise ValueError("Job is not active")
        return record

    def _require_running(self, job_id: str) -> JobRecord:
        record = self._records.get(job_id)
        if record is None or record.status is not JobStatus.RUNNING:
            raise ValueError("Job is not running")
        return record


class FirestoreJobStore:
    """Firestore implementation with transactional claim/cancel transitions."""

    def __init__(self, client: firestore.Client, collection: str) -> None:
        self._collection = client.collection(collection)
        self._client = client

    def _ref(self, job_id: str):
        return self._collection.document(job_id)

    @staticmethod
    def _decode(snapshot) -> JobRecord | None:
        return JobRecord.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def create(self, record: JobRecord) -> None:
        self._ref(record.job_id).create(record.model_dump(mode="json"))

    def get(self, job_id: str) -> JobRecord | None:
        return self._decode(self._ref(job_id).get())

    def claim(self, job_id: str) -> JobRecord | None:
        ref = self._ref(job_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def claim_transaction(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            record = self._decode(snapshot)
            if record is None or record.status is not JobStatus.QUEUED:
                return None
            record.status = JobStatus.RUNNING
            record.started_at = _now()
            transaction.set(ref, record.model_dump(mode="json"))
            return record

        return claim_transaction(transaction, ref)

    def succeed(self, job_id: str, artifact: JobArtifact, *, cache_run_id: str | None, files_scanned: int | None, matches_found: int) -> JobRecord:
        return self._finish(
            job_id,
            status=JobStatus.SUCCEEDED,
            artifact=artifact,
            cache_run_id=cache_run_id,
            files_scanned=files_scanned,
            matches_found=matches_found,
        )

    def fail(self, job_id: str, *, error_code: str, error_message: str) -> JobRecord:
        return self._finish(
            job_id,
            status=JobStatus.FAILED,
            error_code=error_code,
            error_message=error_message[:1000],
        )

    def _finish(self, job_id: str, *, status: JobStatus, **changes) -> JobRecord:
        ref = self._ref(job_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def finish_transaction(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            record = self._decode(snapshot)
            if record is None:
                raise ValueError("Job not found")
            if record.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                return record
            record.status = status
            record.finished_at = _now()
            for name, value in changes.items():
                setattr(record, name, value)
            transaction.set(ref, record.model_dump(mode="json"))
            return record

        return finish_transaction(transaction, ref)

    def cancel_if_queued(self, job_id: str) -> JobRecord | None:
        ref = self._ref(job_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def cancel_transaction(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            record = self._decode(snapshot)
            if record is None or record.status is not JobStatus.QUEUED:
                return None
            record.status = JobStatus.CANCELLED
            record.finished_at = _now()
            transaction.set(ref, record.model_dump(mode="json"))
            return record

        return cancel_transaction(transaction, ref)
