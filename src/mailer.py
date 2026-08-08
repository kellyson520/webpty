"""SMTP delivery for the notifier (stdlib smtplib). Business-management layer."""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


class Mailer:
    def __init__(self, config: dict) -> None:
        # Accept both flat {"smtp": ...} (direct tests) and nested
        # {"notify": {"smtp": ...}} (full app config) layouts.
        smtp = config.get("smtp") or (config.get("notify") or {}).get("smtp") or {}
        self.host = smtp.get("host") or ""
        self.port = int(smtp.get("port") or (465 if smtp.get("tls") else 587))
        self.tls = bool(smtp.get("tls"))
        self.user = smtp.get("user") or ""
        self.password = smtp.get("password") or ""
        self.from_addr = smtp.get("from") or ""
        self.to_addrs = smtp.get("to") or ""

    def enabled(self) -> bool:
        return bool(self.host)

    def _recipients(self) -> list[str]:
        if isinstance(self.to_addrs, str):
            return [a.strip() for a in self.to_addrs.split(",") if a.strip()]
        return list(self.to_addrs or [])

    def send(self, subject: str, html: str) -> None:
        if not self.enabled():
            raise RuntimeError("mailer not configured")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self._recipients())
        msg.attach(MIMEText(html, "html", "utf-8"))
        if self.tls:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=15)
        try:
            if self.user:
                smtp.login(self.user, self.password)
            smtp.sendmail(self.from_addr, self._recipients(), msg.as_string())
        finally:
            smtp.quit()
