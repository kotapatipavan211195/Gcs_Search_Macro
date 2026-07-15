"""Read administrator-configured BigQuery tables for report enrichment."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

import pandas as pd
from google.cloud import bigquery


class EnrichmentError(RuntimeError):
    pass


def load_table(client: bigquery.Client, table_id: str, *, max_rows: int) -> pd.DataFrame:
    """Load a bounded table without interpolating the identifier into SQL."""
    if not table_id:
        return pd.DataFrame()
    table = client.get_table(table_id)
    rows = list(client.list_rows(table, max_results=max_rows + 1))
    if len(rows) > max_rows:
        raise EnrichmentError(f"{table_id} exceeds the configured {max_rows:,}-row report limit")
    if not rows:
        return pd.DataFrame(columns=[field.name for field in table.schema])
    return pd.DataFrame([dict(row.items()) for row in rows])


def load_dag_table(client: bigquery.Client, table_id: str, *, max_rows: int) -> pd.DataFrame:
    return load_table(client, table_id, max_rows=max_rows)


def load_inventory_table(client: bigquery.Client, table_id: str, *, max_rows: int) -> pd.DataFrame:
    return load_table(client, table_id, max_rows=max_rows)


def normalise_gcs_path(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("gs://"):
        text = text[5:]
        _, _, text = text.partition("/")
    return text.strip("/").lower()


@dataclass(frozen=True)
class DagMetadata:
    dag_id: str = ""
    is_active: object = ""
    last_executed: object = ""


class DagLookup(dict[str, DagMetadata]):
    """Direct DAG path lookup with a compact index for suffix fallbacks."""

    def __init__(self, values: dict[str, DagMetadata] | None = None) -> None:
        super().__init__(values or {})
        indexed = sorted((path[::-1], metadata) for path, metadata in self.items())
        self._reversed_paths = tuple(path for path, _ in indexed)
        self._reversed_metadata = tuple(metadata for _, metadata in indexed)

    def unique_suffix_match(self, key: str) -> DagMetadata:
        match: DagMetadata | None = None

        # Find lookup paths that are a segment-aligned suffix of the requested path.
        for index, character in enumerate(key):
            if character != "/":
                continue
            metadata = self.get(key[index + 1:])
            if metadata is None:
                continue
            if match is not None:
                return DagMetadata()
            match = metadata

        # Reversing paths turns "lookup path ends with requested path" into a
        # prefix search. Sorted reversed paths make that search O(log N).
        prefix = f"{key[::-1]}/"
        index = bisect_left(self._reversed_paths, prefix)
        if index < len(self._reversed_paths) and self._reversed_paths[index].startswith(prefix):
            if match is not None:
                return DagMetadata()
            match = self._reversed_metadata[index]
            next_index = index + 1
            if (
                next_index < len(self._reversed_paths)
                and self._reversed_paths[next_index].startswith(prefix)
            ):
                return DagMetadata()

        return match if match is not None else DagMetadata()


def build_dag_lookup(frame: pd.DataFrame) -> DagLookup:
    """Build an unambiguous lookup using common DAG table column names."""
    if frame.empty:
        return DagLookup()
    columns = {str(column).lower(): index for index, column in enumerate(frame.columns)}
    path_column = next((columns[name] for name in ("script_path", "file_path", "gcs_uri", "path") if name in columns), None)
    if path_column is None:
        return DagLookup()
    dag_column = next((columns[name] for name in ("dag_id", "dag_name") if name in columns), None)
    active_column = next((columns[name] for name in ("is_active", "active") if name in columns), None)
    executed_column = next((columns[name] for name in ("last_executed", "last_execution", "last_run") if name in columns), None)
    lookup: dict[str, DagMetadata] = {}
    ambiguous: set[str] = set()
    for row in frame.itertuples(index=False, name=None):
        key = normalise_gcs_path(row[path_column])
        if not key:
            continue
        metadata = DagMetadata(
            dag_id=str(row[dag_column] or "") if dag_column is not None else "",
            is_active=row[active_column] if active_column is not None else "",
            last_executed=row[executed_column] if executed_column is not None else "",
        )
        if key in lookup and lookup[key] != metadata:
            ambiguous.add(key)
        else:
            lookup[key] = metadata
    for key in ambiguous:
        lookup.pop(key, None)
    return DagLookup(lookup)


def find_dag_metadata(lookup: DagLookup, script_path: str) -> DagMetadata:
    key = normalise_gcs_path(script_path)
    direct = lookup.get(key)
    if direct:
        return direct
    return lookup.unique_suffix_match(key)
