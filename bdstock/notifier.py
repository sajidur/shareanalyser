"""Notifications: Windows toast (built in, no extra packages) and optional Telegram bot."""
from __future__ import annotations

import logging
import os
import subprocess
from xml.sax.saxutils import escape, quoteattr

import requests

from . import settings

log = logging.getLogger(__name__)

# The toast XML is built in Python (escaped) and handed to PowerShell through an
# environment variable, so no message text is ever interpreted as script.
_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:BDS_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def toast(title: str, message: str, link: str | None = None) -> bool:
    if os.name != "nt":
        return False
    launch = f" activationType=\"protocol\" launch={quoteattr(link)}" if link else ""
    xml = (
        f"<toast{launch}><visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title[:120])}</text><text>{escape(message[:600])}</text>"
        "</binding></visual></toast>"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_TOAST],
            env=dict(os.environ, BDS_TOAST_XML=xml),
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            log.warning("Toast failed: %s", result.stderr.decode(errors="replace")[:300])
            return False
        return True
    except Exception as e:  # notifications must never break a job
        log.warning("Toast failed: %s", e)
        return False


def telegram(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = token or settings.get("telegram_token")
    chat_id = chat_id or settings.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            log.warning("Telegram failed: HTTP %s %s", r.status_code, r.text[:200])
        return r.ok
    except requests.RequestException as e:
        log.warning("Telegram failed: %s", e)
        return False


def notify(title: str, message: str, path: str = "") -> dict[str, bool]:
    s = settings.get_all()
    link = (s.get("dashboard_url") or "").rstrip("/") + ("/#/" + path.lstrip("/") if path else "/")
    sent = {"toast": False, "telegram": False}
    if s.get("notify_toast") == "1":
        sent["toast"] = toast(title, message, link)
    if s.get("notify_telegram") == "1":
        sent["telegram"] = telegram(f"{title}\n{message}", s.get("telegram_token"), s.get("telegram_chat_id"))
    log.info("Notification '%s' sent: %s", title, sent)
    return sent
