"""Invisible BigQuery cache for reusable search results and source manifests."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from google.cloud import bigquery


_ENSURED_TABLES: set[str] = set()
_ENSURE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SourceManifestEntry:
    bucket: str
    script_path: str
    content_hash: str
    gcs_updated: str
    size_bytes: int
    generation: int = 0
    time_created: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.bucket, self.script_path


@dataclass(frozen=True)
class ManifestDiff:
    changed: tuple[SourceManifestEntry, ...]
    deleted: tuple[tuple[str, str], ...]

    @property
    def is_empty(self) -> bool:
        return not self.changed and not self.deleted


@dataclass(frozen=True)
class CacheRun:
    cache_id: str
    query_key: str
    manifest_fingerprint: str


@dataclass(frozen=True)
class CacheTables:
    project: str
    dataset: str
    prefix: str = ""

    @property
    def dataset_id(self) -> str:
        return f"{self.project}.{self.dataset}"

    def _table(self, name: str) -> str:
        return f"{self.dataset_id}.{self.prefix}{name}"

    @property
    def runs(self) -> str:
        return self._table("search_cache_run")

    @property
    def manifests(self) -> str:
        return self._table("search_cache_manifest")

    @property
    def results(self) -> str:
        return self._table("search_cache_result")

    @property
    def access_log(self) -> str:
        return self._table("search_cache_access")


def query_key(definition: dict) -> str:
    """A stable key for source selection, matching semantics, and terms."""
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def manifest_fingerprint(entries: list[SourceManifestEntry]) -> str:
    encoded = "\n".join(
        "|".join((
            entry.bucket,
            entry.script_path,
            entry.content_hash,
            entry.gcs_updated,
            str(entry.size_bytes),
            str(entry.generation),
            entry.time_created,
        ))
        for entry in sorted(entries, key=lambda item: item.key)
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def diff_manifests(previous: list[SourceManifestEntry], current: list[SourceManifestEntry]) -> ManifestDiff:
    previous_by_key = {entry.key: entry for entry in previous}
    current_by_key = {entry.key: entry for entry in current}
    changed = tuple(
        entry for key, entry in sorted(current_by_key.items())
        if previous_by_key.get(key) != entry
    )
    deleted = tuple(sorted(set(previous_by_key) - set(current_by_key)))
    return ManifestDiff(changed=changed, deleted=deleted)


class BigQuerySearchCache:
    """Append-only cache snapshots; only completed snapshots are eligible for reuse."""

    def __init__(self, bq: bigquery.Client, tables: CacheTables) -> None:
        self._bq = bq
        self._tables = tables

    def ensure_tables(self) -> None:
        identity = f"{self._tables.dataset_id}:{self._tables.prefix}"
        if identity in _ENSURED_TABLES:
            return
        with _ENSURE_LOCK:
            if identity in _ENSURED_TABLES:
                return
            self._ensure_tables()
            _ENSURED_TABLES.add(identity)

    def _ensure_tables(self) -> None:
        self._bq.create_dataset(self._tables.dataset_id, exists_ok=True)
        for statement in (
            f"""
            CREATE TABLE IF NOT EXISTS `{self._tables.runs}` (
              cache_id STRING NOT NULL,
              query_key STRING NOT NULL,
              created_ts TIMESTAMP NOT NULL,
              manifest_fingerprint STRING NOT NULL,
              source_definition_json STRING NOT NULL,
              file_count INT64 NOT NULL,
              match_count INT64 NOT NULL,
              status STRING NOT NULL
            ) PARTITION BY DATE(created_ts) CLUSTER BY query_key, status
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self._tables.manifests}` (
              cache_id STRING NOT NULL,
              bucket STRING NOT NULL,
              script_path STRING NOT NULL,
              content_hash STRING NOT NULL,
              gcs_updated TIMESTAMP,
              size_bytes INT64 NOT NULL,
              generation INT64 NOT NULL,
              time_created TIMESTAMP
            ) CLUSTER BY cache_id, bucket, script_path
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self._tables.results}` (
              cache_id STRING NOT NULL,
              search_term STRING NOT NULL,
              bucket STRING NOT NULL,
              script_path STRING NOT NULL,
              content_hash STRING NOT NULL,
              generation INT64 NOT NULL,
              file_name STRING,
              match_type STRING,
              time_created TIMESTAMP,
              gcs_updated TIMESTAMP,
              size_bytes INT64,
              exact_lines ARRAY<INT64>,
              partial_lines ARRAY<INT64>,
              partial_tokens ARRAY<STRING>
            ) CLUSTER BY cache_id, search_term, bucket
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self._tables.access_log}` (
              access_id STRING NOT NULL,
              access_ts TIMESTAMP NOT NULL,
              job_id STRING NOT NULL,
              requester_hash STRING NOT NULL,
              query_key STRING NOT NULL,
              cache_id STRING NOT NULL,
              reused_cache BOOL NOT NULL,
              refreshed_file_count INT64 NOT NULL
            ) PARTITION BY DATE(access_ts) CLUSTER BY query_key, cache_id
            """,
        ):
            self._bq.query(statement).result()
        # These statements also make an existing deployment forward-compatible
        # with metadata-only filename searches.
        for statement in (
            f"ALTER TABLE `{self._tables.manifests}` ADD COLUMN IF NOT EXISTS time_created TIMESTAMP",
            f"ALTER TABLE `{self._tables.results}` ADD COLUMN IF NOT EXISTS file_name STRING",
            f"ALTER TABLE `{self._tables.results}` ADD COLUMN IF NOT EXISTS match_type STRING",
            f"ALTER TABLE `{self._tables.results}` ADD COLUMN IF NOT EXISTS time_created TIMESTAMP",
            f"ALTER TABLE `{self._tables.results}` ADD COLUMN IF NOT EXISTS gcs_updated TIMESTAMP",
            f"ALTER TABLE `{self._tables.results}` ADD COLUMN IF NOT EXISTS size_bytes INT64",
        ):
            self._bq.query(statement).result()

    def latest_completed_run(self, key: str) -> CacheRun | None:
        rows = list(self._bq.query(
            f"""
            SELECT cache_id, query_key, manifest_fingerprint
            FROM `{self._tables.runs}`
            WHERE query_key = @query_key AND status = 'COMPLETE'
            ORDER BY created_ts DESC
            LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("query_key", "STRING", key),
            ]),
        ).result())
        if not rows:
            return None
        row = rows[0]
        return CacheRun(cache_id=row.cache_id, query_key=row.query_key, manifest_fingerprint=row.manifest_fingerprint)

    def load_manifest(self, cache_id: str) -> list[SourceManifestEntry]:
        rows = self._bq.query(
            f"""
            SELECT bucket, script_path, content_hash, gcs_updated, size_bytes, generation, time_created
            FROM `{self._tables.manifests}`
            WHERE cache_id = @cache_id
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("cache_id", "STRING", cache_id),
            ]),
        ).result()
        return [
            SourceManifestEntry(
                bucket=row.bucket,
                script_path=row.script_path,
                content_hash=row.content_hash,
                gcs_updated=row.gcs_updated.isoformat() if row.gcs_updated else "",
                size_bytes=row.size_bytes,
                generation=row.generation,
                time_created=row.time_created.isoformat() if row.time_created else "",
            )
            for row in rows
        ]

    def load_results(self, cache_id: str, search_terms: list[str]) -> dict[str, list[dict]]:
        results = {term: [] for term in search_terms}
        rows = self._bq.query(
            f"""
            SELECT search_term, bucket, script_path, content_hash, generation,
                   file_name, match_type, time_created, gcs_updated, size_bytes,
                   exact_lines, partial_lines, partial_tokens
            FROM `{self._tables.results}`
            WHERE cache_id = @cache_id
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("cache_id", "STRING", cache_id),
            ]),
        ).result()
        for row in rows:
            if row.search_term not in results:
                continue
            results[row.search_term].append({
                "bucket": row.bucket,
                "file_path": row.script_path,
                "gcs_uri": f"gs://{row.bucket}/{row.script_path}",
                "content_hash": row.content_hash,
                "generation": row.generation,
                "file_name": row.file_name or row.script_path.rsplit("/", 1)[-1],
                "match_type": row.match_type or "",
                "time_created": row.time_created.isoformat() if row.time_created else "",
                "gcs_updated": row.gcs_updated.isoformat() if row.gcs_updated else "",
                "size_bytes": int(row.size_bytes or 0),
                "exact_lines": list(row.exact_lines or []),
                "partial_lines": list(row.partial_lines or []),
                "partial_tokens": set(row.partial_tokens or []),
            })
        return results

    def persist_snapshot(
        self,
        *,
        key: str,
        source_definition: dict,
        manifest: list[SourceManifestEntry],
        results: dict[str, list[dict]],
    ) -> str:
        cache_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        flattened_results = [
            {
                "cache_id": cache_id,
                "search_term": term,
                "bucket": row["bucket"],
                "script_path": row["file_path"],
                "content_hash": row.get("content_hash", ""),
                "generation": int(row.get("generation", 0)),
                "file_name": row.get("file_name"),
                "match_type": row.get("match_type"),
                "time_created": row.get("time_created") or None,
                "gcs_updated": row.get("gcs_updated") or None,
                "size_bytes": int(row.get("size_bytes", 0)),
                "exact_lines": list(row.get("exact_lines", [])),
                "partial_lines": list(row.get("partial_lines", [])),
                "partial_tokens": sorted(row.get("partial_tokens", set())),
            }
            for term, rows in results.items()
            for row in rows
        ]
        self._bq.load_table_from_json([{
            "cache_id": cache_id,
            "query_key": key,
            "created_ts": now,
            "manifest_fingerprint": manifest_fingerprint(manifest),
            "source_definition_json": json.dumps(source_definition, sort_keys=True),
            "file_count": len(manifest),
            "match_count": len(flattened_results),
            "status": "PENDING",
        }], self._tables.runs, job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")).result()
        try:
            if manifest:
                self._bq.load_table_from_json([
                    {
                        "cache_id": cache_id,
                        "bucket": entry.bucket,
                        "script_path": entry.script_path,
                        "content_hash": entry.content_hash,
                        "gcs_updated": entry.gcs_updated or None,
                        "size_bytes": entry.size_bytes,
                        "generation": entry.generation,
                        "time_created": entry.time_created or None,
                    }
                    for entry in manifest
                ], self._tables.manifests, job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")).result()
            if flattened_results:
                self._bq.load_table_from_json(
                    flattened_results, self._tables.results,
                    job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
                ).result()
            self._bq.query(
                f"UPDATE `{self._tables.runs}` SET status = 'COMPLETE' WHERE cache_id = @cache_id",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("cache_id", "STRING", cache_id),
                ]),
            ).result()
        except Exception:
            self._bq.query(
                f"UPDATE `{self._tables.runs}` SET status = 'FAILED' WHERE cache_id = @cache_id",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("cache_id", "STRING", cache_id),
                ]),
            ).result()
            raise
        return cache_id

    def record_access(
        self,
        *,
        job_id: str,
        requester_email: str,
        key: str,
        cache_id: str,
        reused_cache: bool,
        refreshed_file_count: int,
    ) -> None:
        requester_hash = hashlib.sha256(requester_email.lower().encode()).hexdigest()
        self._bq.load_table_from_json([{
            "access_id": str(uuid.uuid4()),
            "access_ts": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "requester_hash": requester_hash,
            "query_key": key,
            "cache_id": cache_id,
            "reused_cache": reused_cache,
            "refreshed_file_count": refreshed_file_count,
        }], self._tables.access_log, job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")).result()
