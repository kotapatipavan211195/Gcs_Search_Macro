"""Cloud Tasks delivery. The task carries only a job ID, never search data."""

from __future__ import annotations

import json
from typing import Protocol

from google.cloud import tasks_v2


class JobQueue(Protocol):
    def enqueue(self, job_id: str) -> None: ...


class CloudTasksJobQueue:
    def __init__(self, client: tasks_v2.CloudTasksClient, *, project: str, location: str, queue: str, worker_url: str, service_account: str) -> None:
        self._client = client
        self._parent = client.queue_path(project, location, queue)
        self._project = project
        self._location = location
        self._queue = queue
        self._worker_url = worker_url.rstrip("/")
        self._service_account = service_account

    def enqueue(self, job_id: str) -> None:
        task = {
            # Deterministic name makes a retried API request idempotent.
            "name": self._client.task_path(self._project, self._location, self._queue, job_id),
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self._worker_url}/internal/jobs/{job_id}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"job_id": job_id}).encode(),
                "oidc_token": {
                    "service_account_email": self._service_account,
                    "audience": self._worker_url,
                },
            },
        }
        self._client.create_task(parent=self._parent, task=task)


class InMemoryJobQueue:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)
