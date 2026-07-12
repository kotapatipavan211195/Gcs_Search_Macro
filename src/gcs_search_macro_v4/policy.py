"""Validate public requests against administrator-owned search policy."""

from __future__ import annotations

import re

from fastapi import HTTPException, status

from gcs_search_macro_v4.models import BucketPath, CopyRequest, CreateJobRequest
from gcs_search_macro_v4.settings import CopyTargetPolicy, ScopePolicy, Settings


def resolve_scope(request: CreateJobRequest, settings: Settings) -> ScopePolicy:
    policy = settings.scope_policies.get(request.scope_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Search scope is not authorized")
    return policy


def resolve_bucket_paths(request: CreateJobRequest, policy: ScopePolicy, settings: Settings) -> list[BucketPath]:
    """
    Resolve editable rows to an allowed set. A user may narrow an approved
    root prefix but cannot pivot to a different bucket or escape that root.
    Empty rows preserve the previous behaviour: search every bucket/path in
    the selected access profile.
    """
    requested = request.bucket_paths or [
        BucketPath(bucket=bucket.name, prefix=bucket.prefix)
        for bucket in policy.buckets
    ]
    if len(requested) > settings.max_bucket_paths:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Too many bucket/path rows")

    seen: set[tuple[str, str]] = set()
    for requested_path in requested:
        identity = (requested_path.bucket.lower(), requested_path.prefix.lower())
        if identity in seen:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Duplicate bucket/path row")
        seen.add(identity)
        if not any(_path_is_within_allowed_root(requested_path, allowed) for allowed in policy.buckets):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Bucket or path is not authorized: {requested_path.bucket}/{requested_path.prefix}",
            )
    return requested


def _path_is_within_allowed_root(requested: BucketPath, allowed) -> bool:
    if requested.bucket.lower() != allowed.name.lower():
        return False
    root = allowed.prefix.strip("/").lower()
    path = requested.prefix.strip("/").lower()
    return not root or path == root or path.startswith(f"{root}/")


def validate_request(request: CreateJobRequest, settings: Settings) -> ScopePolicy:
    """Bound work and accept only a safe, intentionally small regex subset."""
    policy = resolve_scope(request, settings)
    resolve_bucket_paths(request, policy, settings)
    if len(request.terms) > settings.max_terms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Too many search terms")

    seen_terms: set[str] = set()
    for term in request.terms:
        identity = term.value.lower()
        if identity in seen_terms:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Duplicate search term")
        seen_terms.add(identity)
        if len(term.value) > settings.max_term_length:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Search term is too long")
        if request.search_type == "filename" and term.mode != "literal":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Filename search accepts literal filenames only",
            )
        if term.mode == "regex":
            _validate_safe_regex(term.value)
        else:
            _validate_literal(term.value)

    if request.copy_request:
        resolve_copy_target(request.copy_request, policy)
    for recipient in request.email_recipients:
        if "@" not in recipient or not _recipient_is_allowed(recipient, settings):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email recipient is not authorized")
    return policy


def resolve_copy_target(request: CopyRequest, policy: ScopePolicy) -> CopyTargetPolicy:
    """A user may narrow, but never escape, an approved copy bucket/root."""
    for target in policy.copy_targets.values():
        if request.bucket.lower() != target.bucket.lower():
            continue
        root = target.prefix.strip("/").lower()
        path = request.prefix.strip("/").lower()
        if not root or path == root or path.startswith(f"{root}/"):
            return target
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Copy bucket or path is not authorized")


def _recipient_is_allowed(recipient: str, settings: Settings) -> bool:
    return not settings.allowed_domains or recipient.rpartition("@")[2].lower() in settings.allowed_domains


def _validate_safe_regex(pattern: str) -> None:
    """
    Reject constructs that are unnecessary for search and can trigger Python
    backtracking pathologies in the hosted engine. The accepted profile still
    supports examples such as ``pid_\\d+`` and character classes.
    """
    forbidden = ("(", ")", "{", "}")
    if any(token in pattern for token in forbidden) or re.search(r"\\[1-9]", pattern):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Regex groups, counted repetitions, and backreferences are not allowed",
        )
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Invalid regex: {exc}") from exc


def _validate_literal(value: str) -> None:
    if "\x00" in value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Literal terms cannot contain a null character",
        )
