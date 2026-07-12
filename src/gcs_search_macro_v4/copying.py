"""Generation-safe copying of matched source objects to approved GCS roots."""

from __future__ import annotations

import posixpath
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage


@dataclass(frozen=True)
class CopySource:
    bucket: str
    object_name: str
    generation: int = 0


def unique_sources(results: dict[str, list[dict]]) -> list[CopySource]:
    by_key: dict[tuple[str, str], CopySource] = {}
    for rows in results.values():
        for row in rows:
            source = CopySource(
                bucket=str(row["bucket"]),
                object_name=str(row["file_path"]),
                generation=int(row.get("generation", 0)),
            )
            by_key[(source.bucket, source.object_name)] = source
    return [by_key[key] for key in sorted(by_key)]


def copy_sources(
    *,
    project: str,
    sources: list[CopySource],
    target_bucket: str,
    target_prefix: str,
    overwrite: bool,
    max_workers: int,
) -> list[dict]:
    local = threading.local()

    def client() -> storage.Client:
        value = getattr(local, "client", None)
        if value is None:
            value = storage.Client(project=project)
            local.client = value
        return value

    def copy_one(source: CopySource) -> dict:
        destination_name = posixpath.join(target_prefix.strip("/"), source.bucket, source.object_name)
        gcs = client()
        source_bucket = gcs.bucket(source.bucket)
        source_blob = source_bucket.blob(source.object_name, generation=source.generation or None)
        try:
            source_bucket.copy_blob(
                source_blob,
                gcs.bucket(target_bucket),
                new_name=destination_name,
                if_generation_match=None if overwrite else 0,
                if_source_generation_match=source.generation or None,
                timeout=120,
            )
            status = "copied"
            message = ""
        except PreconditionFailed:
            status = "skipped_existing"
            message = "Destination already exists and overwrite is disabled"
        except Exception as exc:
            status = "failed"
            message = str(exc)
        return {
            "source_uri": f"gs://{source.bucket}/{source.object_name}",
            "destination_uri": f"gs://{target_bucket}/{destination_name}",
            "status": status,
            "message": message,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(copy_one, sources))
