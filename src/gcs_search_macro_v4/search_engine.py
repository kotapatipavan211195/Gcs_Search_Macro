"""Native v4 GCS listing and bounded concurrent source search."""

from __future__ import annotations

import re
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Iterable

from google.cloud import storage

from gcs_search_macro_v4.cache import SourceManifestEntry
from gcs_search_macro_v4.models import BucketPath, SearchTerm


PATH_EXCLUDE_PATTERNS = (
    re.compile(r"_old[^/]*\.py$", re.IGNORECASE),
    re.compile(r"_\d{8}\.py$", re.IGNORECASE),
    re.compile(r"_\d{4}[_-]\d{2}[_-]\d{2}\.py$", re.IGNORECASE),
    re.compile(r"(^|/)old[_-]?scripts/", re.IGNORECASE),
    re.compile(r"(^|/)_?old/", re.IGNORECASE),
    re.compile(r"_copy[^/]*\.py$", re.IGNORECASE),
    re.compile(r"(^|/)CHG0", re.IGNORECASE),
)

_HEADER_META_PATTERNS = (
    re.compile(r"^\s*Author[s]?\s*(\(s\))?\s*:", re.IGNORECASE),
    re.compile(r"^\s*Created\s+on\s*:", re.IGNORECASE),
    re.compile(r"^\s*JIRA\s*/?SN\s*#?\s*:", re.IGNORECASE),
    re.compile(r"^\s*Description\s*:", re.IGNORECASE),
    re.compile(r"^\s*Updates?\s*:", re.IGNORECASE),
    re.compile(r"^\s*Scope\s*:", re.IGNORECASE),
    re.compile(r"^\s*Version\s*:", re.IGNORECASE),
    re.compile(r"\([A-Z][0-9]{4,}\)", re.IGNORECASE),
)


class SearchExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompiledTerm:
    source: SearchTerm
    pattern: re.Pattern


def compile_terms(terms: list[SearchTerm]) -> list[CompiledTerm]:
    compiled = []
    for term in terms:
        pattern = re.escape(term.value) if term.mode == "literal" else term.value
        compiled.append(CompiledTerm(source=term, pattern=re.compile(pattern, re.IGNORECASE)))
    return compiled


def strip_header_block(lines: list[str]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    in_header = False
    header_done = False
    quote_char: str | None = None
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not header_done and not in_header and number <= 5:
            for quote in ('"""', "'''"):
                if stripped.startswith(quote):
                    rest = stripped[len(quote):]
                    if rest.endswith(quote) and len(stripped) > len(quote) * 2:
                        header_done = True
                    else:
                        in_header = True
                        quote_char = quote
                    break
            if in_header or header_done and stripped.startswith(('"""', "'''")):
                continue
        if in_header:
            if quote_char and quote_char in stripped:
                in_header = False
                header_done = True
            continue
        if any(pattern.search(line) for pattern in _HEADER_META_PATTERNS):
            continue
        result.append((number, line))
    return result


def _is_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _is_exact_occurrence(line: str, start: int, end: int) -> bool:
    left_is_identifier = start > 0 and _is_identifier_char(line[start - 1])
    right_is_identifier = end < len(line) and _is_identifier_char(line[end])
    return not left_is_identifier and not right_is_identifier


def _surrounding_token(line: str, start: int, end: int) -> str:
    while start > 0 and _is_identifier_char(line[start - 1]):
        start -= 1
    while end < len(line) and _is_identifier_char(line[end]):
        end += 1
    return line[start:end].strip()


def search_text(text: str, terms: list[SearchTerm]) -> dict[str, dict]:
    """Search one decoded file. Both literal and regex terms get exact/partial classification."""
    compiled = compile_terms(terms)
    result = {
        term.source.value: {"exact_lines": set(), "partial_lines": set(), "partial_tokens": set()}
        for term in compiled
    }
    for line_number, line in strip_header_block(text.splitlines()):
        for term in compiled:
            for match in term.pattern.finditer(line):
                # Ignore zero-length regex matches; they do not identify text.
                if match.start() == match.end():
                    continue
                target = result[term.source.value]
                if _is_exact_occurrence(line, match.start(), match.end()):
                    target["exact_lines"].add(line_number)
                else:
                    target["partial_lines"].add(line_number)
                    token = _surrounding_token(line, match.start(), match.end())
                    if token:
                        target["partial_tokens"].add(token)
    return {
        value: {
            "exact_lines": sorted(data["exact_lines"]),
            "partial_lines": sorted(data["partial_lines"]),
            "partial_tokens": data["partial_tokens"],
        }
        for value, data in result.items()
        if data["exact_lines"] or data["partial_lines"]
    }


class SearchEngine:
    def __init__(
        self,
        project: str,
        *,
        max_workers: int,
        max_files: int,
        max_file_bytes: int,
        max_result_rows: int,
    ) -> None:
        self.project = project
        self.max_workers = max_workers
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_result_rows = max_result_rows
        self._local = threading.local()

    def _client(self) -> storage.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = storage.Client(project=self.project)
            self._local.client = client
        return client

    def list_manifest(
        self,
        bucket_paths: list[BucketPath],
        *,
        exclude_keywords: list[str],
        exclude_patterns: list[str],
    ) -> list[SourceManifestEntry]:
        patterns = [re.compile(pattern, re.IGNORECASE) for pattern in exclude_patterns]
        keywords = [keyword.lower() for keyword in exclude_keywords]
        client = storage.Client(project=self.project)
        by_key: dict[tuple[str, str], SourceManifestEntry] = {}
        for source in bucket_paths:
            for blob in client.list_blobs(source.bucket, prefix=source.prefix):
                name = blob.name
                lower_name = name.lower()
                if not lower_name.endswith(".py"):
                    continue
                if any(keyword in lower_name for keyword in keywords):
                    continue
                if any(pattern.search(name) for pattern in patterns):
                    continue
                if any(pattern.search(name) for pattern in PATH_EXCLUDE_PATTERNS):
                    continue
                key = (source.bucket, name)
                if key in by_key:
                    continue
                if len(by_key) >= self.max_files:
                    raise SearchExecutionError(f"Search scope exceeds the {self.max_files:,}-file limit")
                size = blob.size or 0
                if size > self.max_file_bytes:
                    raise SearchExecutionError(
                        f"gs://{source.bucket}/{name} exceeds the {self.max_file_bytes:,}-byte file limit"
                    )
                by_key[key] = SourceManifestEntry(
                    bucket=source.bucket,
                    script_path=name,
                    content_hash=blob.crc32c or "",
                    gcs_updated=blob.updated.isoformat() if blob.updated else "",
                    size_bytes=size,
                    generation=int(blob.generation or 0),
                    time_created=blob.time_created.isoformat() if blob.time_created else "",
                )
        return sorted(by_key.values(), key=lambda entry: entry.key)

    def list_filename_manifest(self, bucket_paths: list[BucketPath]) -> list[SourceManifestEntry]:
        """List file metadata for every extension without downloading objects."""
        client = storage.Client(project=self.project)
        by_key: dict[tuple[str, str], SourceManifestEntry] = {}
        for source in bucket_paths:
            for blob in client.list_blobs(source.bucket, prefix=source.prefix):
                if not blob.name or blob.name.endswith("/"):
                    continue
                key = (source.bucket, blob.name)
                if key in by_key:
                    continue
                if len(by_key) >= self.max_files:
                    raise SearchExecutionError(f"Search scope exceeds the {self.max_files:,}-file limit")
                by_key[key] = SourceManifestEntry(
                    bucket=source.bucket,
                    script_path=blob.name,
                    content_hash=blob.crc32c or "",
                    gcs_updated=blob.updated.isoformat() if blob.updated else "",
                    size_bytes=blob.size or 0,
                    generation=int(blob.generation or 0),
                    time_created=blob.time_created.isoformat() if blob.time_created else "",
                )
        return sorted(by_key.values(), key=lambda entry: entry.key)

    def search_filenames(
        self,
        manifest: Iterable[SourceManifestEntry],
        terms: list[SearchTerm],
    ) -> dict[str, list[dict]]:
        """Classify case-insensitive basename equality or containment."""
        results = {term.value: [] for term in terms}
        result_rows = 0
        for entry in manifest:
            file_name = entry.script_path.rsplit("/", 1)[-1]
            folded_name = file_name.casefold()
            for term in terms:
                folded_term = term.value.casefold()
                if folded_name == folded_term:
                    match_type = "exact"
                elif folded_term in folded_name:
                    match_type = "partial"
                else:
                    continue
                results[term.value].append({
                    "bucket": entry.bucket,
                    "file_path": entry.script_path,
                    "file_name": file_name,
                    "gcs_uri": f"gs://{entry.bucket}/{entry.script_path}",
                    "content_hash": entry.content_hash,
                    "generation": entry.generation,
                    "match_type": match_type,
                    "time_created": entry.time_created,
                    "gcs_updated": entry.gcs_updated,
                    "size_bytes": entry.size_bytes,
                    "exact_lines": [],
                    "partial_lines": [],
                    "partial_tokens": set(),
                })
                result_rows += 1
                if result_rows > self.max_result_rows:
                    raise SearchExecutionError(
                        f"Search exceeds the configured {self.max_result_rows:,}-row report limit"
                    )
        for rows in results.values():
            rows.sort(key=lambda row: (row["bucket"], row["file_path"]))
        return results

    def _search_one(
        self,
        entry: SourceManifestEntry,
        terms: list[SearchTerm],
    ) -> tuple[SourceManifestEntry, dict[str, dict]]:
        try:
            blob = self._client().bucket(entry.bucket).blob(
                entry.script_path,
                generation=entry.generation or None,
            )
            raw = blob.download_as_bytes(
                if_generation_match=entry.generation or None,
                timeout=60,
            )
        except Exception as exc:
            raise SearchExecutionError(f"Unable to read gs://{entry.bucket}/{entry.script_path}: {exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        return entry, search_text(text, terms)

    def search(self, manifest: Iterable[SourceManifestEntry], terms: list[SearchTerm]) -> dict[str, list[dict]]:
        entries = list(manifest)
        request_terms = list(terms)
        results = {term.value: [] for term in request_terms}
        result_rows = 0
        search_one = lambda entry: self._search_one(entry, request_terms)
        for entry, matches in _bounded_parallel_map(search_one, entries, self.max_workers):
            for term, data in matches.items():
                results[term].append({
                    "bucket": entry.bucket,
                    "file_path": entry.script_path,
                    "gcs_uri": f"gs://{entry.bucket}/{entry.script_path}",
                    "content_hash": entry.content_hash,
                    "generation": entry.generation,
                    "exact_lines": data["exact_lines"],
                    "partial_lines": data["partial_lines"],
                    "partial_tokens": data["partial_tokens"],
                })
                result_rows += 1
                if result_rows > self.max_result_rows:
                    raise SearchExecutionError(
                        f"Search exceeds the configured {self.max_result_rows:,}-row report limit"
                    )
        for rows in results.values():
            rows.sort(key=lambda row: (row["bucket"], row["file_path"]))
        return results


def _bounded_parallel_map(
    function: Callable[[SourceManifestEntry], tuple[SourceManifestEntry, dict]],
    entries: list[SourceManifestEntry],
    max_workers: int,
) -> Iterable[tuple[SourceManifestEntry, dict]]:
    """Bound in-flight Futures to avoid O(file count) executor memory."""
    iterator = iter(entries)
    pending: set[Future] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for _ in range(max_workers * 2):
            try:
                pending.add(pool.submit(function, next(iterator)))
            except StopIteration:
                break
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
                try:
                    pending.add(pool.submit(function, next(iterator)))
                except StopIteration:
                    pass
