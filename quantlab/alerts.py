"""Alerts: things worth knowing about, shown in the app and optionally pushed to you.

Every alert is saved and listed on the Dashboard. To also receive them away from
the app, configure either or both of:

* Phone push through ntfy (https://ntfy.sh, free app, no account): set
  ``QUANTLAB_NTFY_TOPIC`` to a long, hard-to-guess topic name and subscribe to the
  same topic in the ntfy app. Anyone who knows the topic can read it, so treat it
  like a password.
* Email: set ``QUANTLAB_SMTP_HOST``, ``QUANTLAB_SMTP_USER``, ``QUANTLAB_SMTP_PASSWORD``
  and ``QUANTLAB_ALERT_EMAIL`` (port 587 with STARTTLS unless ``QUANTLAB_SMTP_PORT``
  says otherwise).

Delivery problems never interrupt the app; they are recorded on the alert.
"""
from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from .db import connect


def _env(name: str) -> str:
    return os.environ.get(f"QUANTLAB_{name}", "").strip()


def channels() -> dict:
    return {"ntfy": bool(_env("NTFY_TOPIC")), "email": bool(_env("SMTP_HOST") and _env("ALERT_EMAIL"))}


def _send_ntfy(title: str, message: str) -> None:
    import requests

    server = _env("NTFY_SERVER") or "https://ntfy.sh"
    resp = requests.post(f"{server.rstrip('/')}/{_env('NTFY_TOPIC')}", data=message.encode("utf-8"),
                         headers={"Title": title, "Tags": "chart_with_upwards_trend"}, timeout=10)
    resp.raise_for_status()


def _send_email(title: str, message: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, _env("SMTP_USER") or _env("ALERT_EMAIL"), _env("ALERT_EMAIL")
    msg.set_content(message)
    with smtplib.SMTP(_env("SMTP_HOST"), int(_env("SMTP_PORT") or 587), timeout=15) as smtp:
        smtp.starttls()
        if _env("SMTP_USER"):
            smtp.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        smtp.send_message(msg)


def raise_alert(kind: str, symbol: str | None, message: str) -> dict:
    """Record an alert and push it to any configured channel."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    failures = []
    title = f"QuantLab: {symbol}" if symbol else "QuantLab"
    active = channels()
    for name, send in (("ntfy", _send_ntfy), ("email", _send_email)):
        if active[name]:
            try:
                send(title, message)
            except Exception as exc:
                failures.append(f"{name} delivery failed: {exc}")
    stored = message + (" (" + "; ".join(failures) + ")" if failures else "")
    with connect() as conn:
        cur = conn.execute("INSERT INTO alerts (ts, kind, symbol, message) VALUES (?,?,?,?)", (ts, kind, symbol, stored))
    return {"id": cur.lastrowid, "ts": ts, "kind": kind, "symbol": symbol, "message": stored}


def recent(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]


def mark_read() -> None:
    with connect() as conn:
        conn.execute("UPDATE alerts SET read=1 WHERE read=0")


def clear() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM alerts")
