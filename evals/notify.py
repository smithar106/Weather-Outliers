"""Render and email the evaluation summary over SMTP.

Delivery is best-effort and never affects the evaluation's own verdict: the exit
code of ``evals.deliver`` belongs to the evaluation, not to the mail transport.
A missing SMTP configuration is a skip, and a transport failure is a warning.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate

from evals.harness import Report

logger = logging.getLogger(__name__)

_STATUS_MARK = {"passed": "PASS", "failed": "FAIL", "error": "ERROR"}


def render_email_body(report: Report) -> str:
    """A plain-text summary: the same numbers ``print_summary`` shows, for email."""
    payload = report.to_dict()
    totals = payload["totals"]

    lines = [
        f"Weather Outliers evaluation — {report.status.upper()}",
        f"generated {report.generated_at}",
        f"commit {report.git_commit or 'unknown'}"
        + (" (uncommitted changes)" if report.git_dirty else ""),
        f"methodology {report.methodology_version}",
        "",
    ]

    width = max((len(suite["title"]) for suite in payload["suites"]), default=10)
    for suite in payload["suites"]:
        mark = _STATUS_MARK.get(suite["status"], suite["status"].upper())
        lines.append(
            f"  {mark:<5} {suite['title']:<{width}}  "
            f"{suite['cases_passed']}/{suite['cases_total']} checks  "
            f"{suite['duration_ms']} ms"
        )
        if suite["error"]:
            for err_line in suite["error"].strip().splitlines()[-3:]:
                lines.append(f"        {err_line}")

    lines += [
        "",
        f"  {totals['suites_passed']}/{totals['suites_total']} suites, "
        f"{totals['cases_passed']}/{totals['cases_total']} checks, "
        f"{totals['duration_ms']} ms total",
        "",
    ]
    return "\n".join(lines)


def send_email(
    body: str,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipient: str,
    tls: bool,
    timeout: float = 30.0,
) -> None:
    """Send ``body`` as a plain-text email. Raises on transport failure."""
    message = EmailMessage()
    message["Subject"] = "Weather Outliers evaluation"
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=timeout) as server:
        if tls:
            server.starttls()
        if username:
            server.login(username, password)
        server.send_message(message)


def notify(report: Report, settings) -> bool:
    """Email the summary if SMTP is configured. Returns whether it was sent."""
    if not settings.smtp_configured:
        logger.info("SMTP not configured; skipping email")
        return False
    try:
        send_email(
            render_email_body(report),
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender=settings.smtp_from,
            recipient=settings.eval_email_to,
            tls=settings.smtp_tls,
        )
    except Exception as exc:  # pragma: no cover - transport failure, not a crash
        logger.warning("email delivery failed: %s", exc)
        return False
    return True
