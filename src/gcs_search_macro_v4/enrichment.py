"""Read administrator-configured BigQuery tables for report enrichment."""

from __future__ import annotations

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


def build_dag_lookup(frame: pd.DataFrame) -> dict[str, DagMetadata]:
    """Build an unambiguous lookup using common DAG table column names."""
    if frame.empty:
        return {}
    columns = {str(column).lower(): str(column) for column in frame.columns}
    path_column = next((columns[name] for name in ("script_path", "file_path", "gcs_uri", "path") if name in columns), None)
    if not path_column:
        return {}
    dag_column = next((columns[name] for name in ("dag_id", "dag_name") if name in columns), None)
    active_column = next((columns[name] for name in ("is_active", "active") if name in columns), None)
    executed_column = next((columns[name] for name in ("last_executed", "last_execution", "last_run") if name in columns), None)
    lookup: dict[str, DagMetadata] = {}
    ambiguous: set[str] = set()
    for _, row in frame.iterrows():
        key = normalise_gcs_path(row.get(path_column))
        if not key:
            continue
        metadata = DagMetadata(
            dag_id=str(row.get(dag_column, "") or "") if dag_column else "",
            is_active=row.get(active_column, "") if active_column else "",
            last_executed=row.get(executed_column, "") if executed_column else "",
        )
        if key in lookup and lookup[key] != metadata:
            ambiguous.add(key)
        else:
            lookup[key] = metadata
    for key in ambiguous:
        lookup.pop(key, None)
    return lookup


def find_dag_metadata(lookup: dict[str, DagMetadata], script_path: str) -> DagMetadata:
    key = normalise_gcs_path(script_path)
    direct = lookup.get(key)
    if direct:
        return direct
    suffix_matches = [metadata for path, metadata in lookup.items() if key.endswith(f"/{path}") or path.endswith(f"/{key}")]
    return suffix_matches[0] if len(suffix_matches) == 1 else DagMetadata()
