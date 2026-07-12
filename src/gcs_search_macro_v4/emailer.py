"""Hosted report delivery with recipient restrictions enforced before sending."""

from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from gcs_search_macro_v4.settings import Settings


class EmailDeliveryError(RuntimeError):
    pass


def send_report(settings: Settings, *, recipients: list[str], report_path: Path, job_id: str) -> None:
    if not recipients:
        return
    if not settings.smtp_host:
        raise EmailDeliveryError("Email delivery is not configured")

    from_address = settings.smtp_from or settings.smtp_user
    if not from_address:
        raise EmailDeliveryError("A SMTP From address is required for report delivery")

    msg = EmailMessage()
    msg["Subject"] = f"GCS Search report {job_id}"
    msg["From"] = from_address
    msg["To"] = ", ".join(recipients)
    msg.set_content("Your requested GCS Search report is attached.")
    content_type, encoding = mimetypes.guess_type(report_path.name)
    if not content_type or encoding:
        content_type = "application/octet-stream"
    maintype, subtype = content_type.split("/", 1)
    msg.add_attachment(report_path.read_bytes(), maintype=maintype, subtype=subtype, filename="gcs-search-results.xlsx")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if settings.smtp_use_tls:
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
