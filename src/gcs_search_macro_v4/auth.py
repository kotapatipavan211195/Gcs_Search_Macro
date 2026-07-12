"""Request identity handling for IAP-protected Cloud Run ingress."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from gcs_search_macro_v4.models import Requester
from gcs_search_macro_v4.settings import Settings, get_settings

_IAP_PREFIX = "accounts.google.com:"


def _normalise_iap_email(raw_email: str) -> str:
    email = raw_email.strip().lower()
    if email.startswith(_IAP_PREFIX):
        email = email[len(_IAP_PREFIX):]
    return email


def _is_allowed_domain(email: str, settings: Settings) -> bool:
    if not settings.allowed_domains:
        return True
    return email.rpartition("@")[2] in settings.allowed_domains


def get_requester(request: Request) -> Requester:
    """
    Trust IAP's identity header only when the deployment prevents direct
    Cloud Run ingress.  `deploy/README.md` makes that topology mandatory.

    The development header is intentionally rejected outside local/test mode.
    """
    settings = get_settings()
    if settings.app_env in {"development", "test"}:
        email = request.headers.get("X-Dev-User-Email", "").strip().lower()
        source = "development"
    else:
        email = _normalise_iap_email(request.headers.get("X-Goog-Authenticated-User-Email", ""))
        source = "iap"

    if not email or "@" not in email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authenticated user identity required")
    if not _is_allowed_domain(email, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User domain is not authorized")
    return Requester(email=email, auth_source=source)
