"""Native production Excel report writer."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font, PatternFill

from gcs_search_macro_v4.enrichment import build_dag_lookup, find_dag_metadata


MATCH_COLUMNS = [
    "search_term",
    "match_type",
    "source_bucket",
    "file_path",
    "gcs_uri",
    "exact_lines",
    "partial_lines",
    "partial_matches",
    "dag_id",
    "is_active",
    "last_executed",
]

FILENAME_COLUMNS = [
    "search_term",
    "match_type",
    "file_name",
    "source_bucket",
    "blob_name",
    "created_or_landed_at_utc",
    "last_updated_utc",
    "size_bytes",
    "size_mib",
    "gcs_uri",
]


def _excel_text(value: object, limit: int = 32_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit - 14]}… [truncated]"


def _match_rows(results: dict[str, list[dict]], dag_df: pd.DataFrame) -> list[dict]:
    lookup = build_dag_lookup(dag_df)
    rows: list[dict] = []
    for term, matches in results.items():
        for match in matches:
            metadata = find_dag_metadata(lookup, match["file_path"])
            exact = list(match.get("exact_lines", []))
            partial = list(match.get("partial_lines", []))
            if exact:
                match_type = "exact" if not partial else "exact and partial"
            else:
                match_type = "partial"
            rows.append({
                "search_term": term,
                "match_type": match_type,
                "source_bucket": match["bucket"],
                "file_path": match["file_path"],
                "gcs_uri": match.get("gcs_uri", f"gs://{match['bucket']}/{match['file_path']}"),
                "exact_lines": _excel_text(", ".join(map(str, exact))),
                "partial_lines": _excel_text(", ".join(map(str, partial))),
                "partial_matches": _excel_text(", ".join(sorted(match.get("partial_tokens", set())))),
                "dag_id": metadata.dag_id,
                "is_active": metadata.is_active,
                "last_executed": metadata.last_executed,
            })
    return sorted(rows, key=lambda row: (row["search_term"].lower(), row["source_bucket"], row["file_path"]))


def _sheet_name(term: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", term).strip() or "Term"
    base = base[:31]
    candidate = base
    counter = 2
    while candidate.lower() in used:
        suffix = f"_{counter}"
        candidate = f"{base[:31 - len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def write_report(
    *,
    results: dict[str, list[dict]],
    output_path: Path,
    dag_df: pd.DataFrame,
    inventory_df: pd.DataFrame,
    copy_report: list[dict] | None,
) -> None:
    rows = _match_rows(results, dag_df)
    all_matches = pd.DataFrame(rows, columns=MATCH_COLUMNS)
    summary = pd.DataFrame([
        {
            "search_term": term,
            "matching_files": len(matches),
            "files_with_exact_matches": sum(bool(row.get("exact_lines")) for row in matches),
            "files_with_partial_matches": sum(bool(row.get("partial_lines")) for row in matches),
        }
        for term, matches in results.items()
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    used = {"all_matches", "summary", "job_inventory", "copied_files", "no_matches"}
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        if rows:
            all_matches.to_excel(writer, sheet_name="All_Matches", index=False)
            for term in results:
                term_rows = all_matches[all_matches["search_term"] == term]
                term_rows.to_excel(writer, sheet_name=_sheet_name(term, used), index=False)
        else:
            pd.DataFrame([{"message": "No matching files found."}]).to_excel(
                writer, sheet_name="No_Matches", index=False
            )
        if not inventory_df.empty:
            inventory_df.to_excel(writer, sheet_name="Job_Inventory", index=False)
        if copy_report is not None:
            pd.DataFrame(copy_report, columns=["source_uri", "destination_uri", "status", "message"]).to_excel(
                writer, sheet_name="Copied_Files", index=False
            )
        _style_workbook(writer.book)


def write_filename_report(
    *,
    results: dict[str, list[dict]],
    output_path: Path,
    copy_report: list[dict] | None,
) -> None:
    rows = [
        {
            "search_term": term,
            "match_type": match["match_type"],
            "file_name": match.get("file_name") or match["file_path"].rsplit("/", 1)[-1],
            "source_bucket": match["bucket"],
            "blob_name": match["file_path"],
            "created_or_landed_at_utc": match.get("time_created", ""),
            "last_updated_utc": match.get("gcs_updated", ""),
            "size_bytes": int(match.get("size_bytes", 0)),
            "size_mib": round(int(match.get("size_bytes", 0)) / (1024 * 1024), 3),
            "gcs_uri": match.get("gcs_uri", f"gs://{match['bucket']}/{match['file_path']}"),
        }
        for term, matches in results.items()
        for match in matches
    ]
    rows.sort(key=lambda row: (row["search_term"].lower(), row["source_bucket"], row["blob_name"]))
    all_files = pd.DataFrame(rows, columns=FILENAME_COLUMNS)
    summary = pd.DataFrame([
        {
            "search_term": term,
            "matching_files": len(matches),
            "exact_filename_matches": sum(row.get("match_type") == "exact" for row in matches),
            "partial_filename_matches": sum(row.get("match_type") == "partial" for row in matches),
        }
        for term, matches in results.items()
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    used = {"all_files", "summary", "copied_files", "no_matches"}
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        if rows:
            all_files.to_excel(writer, sheet_name="All_Files", index=False)
            for term in results:
                term_rows = all_files[all_files["search_term"] == term]
                term_rows.to_excel(writer, sheet_name=_sheet_name(term, used), index=False)
        else:
            pd.DataFrame([{"message": "No matching filenames found."}]).to_excel(
                writer, sheet_name="No_Matches", index=False
            )
        if copy_report is not None:
            pd.DataFrame(copy_report, columns=["source_uri", "destination_uri", "status", "message"]).to_excel(
                writer, sheet_name="Copied_Files", index=False
            )
        _style_workbook(writer.book)


def _style_workbook(workbook) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(color="FFFFFF", bold=True)
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.fill = fill
            cell.font = font
        for column in worksheet.columns:
            values = [str(cell.value or "") for cell in column[:200]]
            worksheet.column_dimensions[column[0].column_letter].width = min(max(map(len, values), default=8) + 2, 60)
