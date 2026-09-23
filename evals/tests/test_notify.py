"""Rendering and sending the evaluation email."""

from __future__ import annotations

from unittest import mock

from evals.notify import notify, render_email_body, send_email


def test_render_email_body_contains_headline_numbers(report):
    body = render_email_body(report)
    assert "FAILED" in body
    assert "1/2 suites" in body
    assert "2/3 checks" in body
    assert "commit abc123" in body
    assert "methodology 1.0.0" in body


def test_render_email_body_marks_a_dirty_tree(report):
    from dataclasses import replace

    body = render_email_body(replace(report, git_dirty=True))
    assert "uncommitted changes" in body


def test_send_email_drives_smtp():
    with mock.patch("evals.notify.smtplib.SMTP") as smtp:
        send_email(
            "body",
            host="smtp.example.com",
            port=587,
            username="u",
            password="p",
            sender="from@example.com",
            recipient="to@example.com",
            tls=True,
        )
    smtp.assert_called_once_with("smtp.example.com", 587, timeout=30.0)
    server = smtp.return_value.__enter__.return_value
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("u", "p")
    server.send_message.assert_called_once()


def test_notify_skips_when_unconfigured(report):
    settings = mock.Mock(smtp_configured=False)
    with mock.patch("evals.notify.send_email") as send:
        assert notify(report, settings) is False
    send.assert_not_called()


def test_notify_sends_when_configured(report):
    settings = mock.Mock(
        smtp_configured=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="from@example.com",
        eval_email_to="to@example.com",
        smtp_tls=True,
    )
    with mock.patch("evals.notify.send_email") as send:
        assert notify(report, settings) is True
    send.assert_called_once()
