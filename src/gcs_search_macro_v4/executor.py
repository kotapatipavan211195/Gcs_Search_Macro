"""Standalone v4 job execution: GCS search, BigQuery cache, copy, and report."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from google.cloud import bigquery

from gcs_search_macro_v4.cache import BigQuerySearchCache, CacheTables, diff_manifests, query_key
from gcs_search_macro_v4.copying import copy_sources, unique_sources
from gcs_search_macro_v4.enrichment import load_dag_table, load_inventory_table
from gcs_search_macro_v4.models import BucketPath, JobRecord
from gcs_search_macro_v4.policy import resolve_bucket_paths, resolve_copy_target
from gcs_search_macro_v4.reporting import write_filename_report, write_report
from gcs_search_macro_v4.search_engine import SearchEngine
from gcs_search_macro_v4.settings import ScopePolicy, Settings


@dataclass(frozen=True)
class ExecutionResult:
    report_path: Path
    cache_run_id: str
    files_scanned: int
    matches_found: int


class ProductionExecutor:
    """Execute one authorized job without importing or reading another project version."""

    def execute(self, job: JobRecord, policy: ScopePolicy, settings: Settings, work_dir: Path) -> ExecutionResult:
        bucket_paths = resolve_bucket_paths(job.request, policy, settings)
        engine = SearchEngine(
            policy.project,
            max_workers=settings.search_workers,
            max_files=settings.max_files_per_job,
            max_file_bytes=settings.max_file_bytes,
            max_result_rows=settings.max_result_rows,
        )
        if job.request.search_type == "filename":
            manifest = engine.list_filename_manifest(bucket_paths)
            run_search = engine.search_filenames
        else:
            manifest = engine.list_manifest(
                bucket_paths,
                exclude_keywords=policy.exclude_keywords,
                exclude_patterns=policy.exclude_patterns,
            )
            run_search = engine.search
        definition = self._cache_definition(policy, bucket_paths, job)
        key = query_key(definition)
        bq = bigquery.Client(project=policy.project)
        cache = BigQuerySearchCache(
            bq,
            CacheTables(project=policy.project, dataset=settings.cache_dataset, prefix=settings.cache_table_prefix),
        )
        cache.ensure_tables()
        latest = cache.latest_completed_run(key)
        refreshed_file_count = 0
        reused_cache = False

        if latest is None:
            results = run_search(manifest, job.request.terms)
            refreshed_file_count = len(manifest)
            cache_run_id = cache.persist_snapshot(
                key=key,
                source_definition=definition,
                manifest=manifest,
                results=results,
            )
        else:
            previous_manifest = cache.load_manifest(latest.cache_id)
            changes = diff_manifests(previous_manifest, manifest)
            if changes.is_empty:
                results = cache.load_results(latest.cache_id, [term.value for term in job.request.terms])
                cache_run_id = latest.cache_id
                reused_cache = True
            else:
                cached = cache.load_results(latest.cache_id, [term.value for term in job.request.terms])
                refreshed = run_search(changes.changed, job.request.terms)
                results = self._merge_refreshed_results(cached, refreshed, changes.changed, changes.deleted)
                refreshed_file_count = len(changes.changed)
                cache_run_id = cache.persist_snapshot(
                    key=key,
                    source_definition=definition,
                    manifest=manifest,
                    results=results,
                )

        if sum(len(rows) for rows in results.values()) > settings.max_result_rows:
            raise ValueError(f"Search exceeds the configured {settings.max_result_rows:,}-row report limit")

        cache.record_access(
            job_id=job.job_id,
            requester_email=job.owner_email,
            key=key,
            cache_id=cache_run_id,
            reused_cache=reused_cache,
            refreshed_file_count=refreshed_file_count,
        )

        copy_report = self._copy_matches(job, policy, settings, results)
        report_path = work_dir / "gcs-search-results.xlsx"
        if job.request.search_type == "filename":
            write_filename_report(
                results=results,
                output_path=report_path,
                copy_report=copy_report,
            )
        else:
            dag_df = load_dag_table(bq, policy.dag_table, max_rows=settings.max_inventory_rows)
            inventory_df = load_inventory_table(
                bq,
                policy.job_inventory_table,
                max_rows=settings.max_inventory_rows,
            )
            write_report(
                results=results,
                output_path=report_path,
                dag_df=dag_df,
                inventory_df=inventory_df,
                copy_report=copy_report,
            )
        unique_matches = {
            (row.get("bucket", ""), row.get("file_path", ""))
            for rows in results.values()
            for row in rows
        }
        return ExecutionResult(
            report_path=report_path,
            cache_run_id=cache_run_id,
            files_scanned=refreshed_file_count,
            matches_found=len(unique_matches),
        )

    @staticmethod
    def _cache_definition(policy: ScopePolicy, bucket_paths: list[BucketPath], job: JobRecord) -> dict:
        definition = {
            "cache_format": 3,
            "search_type": job.request.search_type,
            "project": policy.project,
            "buckets": [bucket.model_dump() for bucket in bucket_paths],
            "terms": [term.model_dump() for term in job.request.terms],
        }
        if job.request.search_type == "content":
            definition.update({
                "exclude_keywords": sorted(policy.exclude_keywords),
                "exclude_patterns": sorted(policy.exclude_patterns),
            })
        return definition

    @staticmethod
    def _merge_refreshed_results(cached, refreshed, changed, deleted) -> dict[str, list[dict]]:
        affected = {entry.key for entry in changed} | set(deleted)
        return {
            term: sorted(
                [
                    row for row in cached.get(term, [])
                    if (row.get("bucket", ""), row.get("file_path", "")) not in affected
                ] + refreshed.get(term, []),
                key=lambda row: (row.get("bucket", ""), row.get("file_path", "")),
            )
            for term in cached
        }

    @staticmethod
    def _copy_matches(job: JobRecord, policy: ScopePolicy, settings: Settings, results) -> list[dict] | None:
        request = job.request.copy_request
        if request is None:
            return None
        resolve_copy_target(request, policy)
        root = request.prefix.strip("/")
        target_prefix = f"{root}/jobs/{job.job_id}".strip("/")
        return copy_sources(
            project=policy.project,
            sources=unique_sources(results),
            target_bucket=request.bucket,
            target_prefix=target_prefix,
            overwrite=request.overwrite,
            max_workers=settings.copy_workers,
        )
