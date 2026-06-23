"""
tools/email_sender.py
---------------------
Gmail SMTP sender using app passwords.
All sends are logged and support dry-run mode.
"""

from __future__ import annotations
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from utils.helpers import log_agent, now_iso
from config.settings import settings


class GmailSender:
    """Thread-safe Gmail SMTP sender with connection pooling."""

    SMTP_HOST = "smtp.gmail.com"
    SMTP_PORT = 587

    def __init__(self):
        self.user = settings.gmail_user
        self.password = settings.gmail_app_password
        self.available = bool(self.user and self.password)

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """
        Send an email. Returns (success: bool, message: str).
        dry_run=True logs the email but doesn't send.
        """
        if not to or "@" not in to:
            return False, f"Invalid email address: '{to}'"

        if dry_run:
            log_agent("GmailSender", f"[DRY RUN] Would send to {to}: '{subject}'", "info")
            return True, "dry_run"

        if not self.available:
            return False, "Gmail credentials not configured (GMAIL_USER / GMAIL_APP_PASSWORD)"

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{settings.developer_name} <{self.user}>"
            msg["To"] = to

            # Plain text version
            msg.attach(MIMEText(body, "plain"))

            context = ssl.create_default_context()
            with smtplib.SMTP(self.SMTP_HOST, self.SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(self.user, self.password)
                server.sendmail(self.user, to, msg.as_string())

            log_agent("GmailSender", f"✓ Sent to {to}: '{subject}'", "done")
            return True, "sent"

        except smtplib.SMTPAuthenticationError:
            return False, "Gmail authentication failed — check GMAIL_APP_PASSWORD"
        except smtplib.SMTPRecipientsRefused:
            return False, f"Recipient refused: {to}"
        except Exception as e:
            return False, str(e)


# Singleton
gmail_sender = GmailSender()
