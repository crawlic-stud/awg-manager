from __future__ import annotations

import html
import os
import re
from typing import Any

from jinja2 import Template

DEFAULT_EMAIL_SUBJECT_TEMPLATE = "Доступ к {{ server_name }} для {{ name }}"
DEFAULT_EMAIL_TEXT_TEMPLATE = """Привет, {{ name }}!

Высылаю VPN-доступ для {{ name }} к серверу {{ server_name }}.

Актуальные ссылки для скачивания:
Официальный сайт: https://amnezia.org/downloads
Зеркало: https://storage.googleapis.com/amnezia/amnezia.org

Ключ для подключения:
{{ vpn_link }}

Не передавайте эту ссылку третьим лицам. Если нужен новый ключ, просто ответьте на это письмо.
"""

DEFAULT_EMAIL_BODY_TEMPLATE = """<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.5;color:#0f172a">
  <p>Привет, {{ name }}! Высылаю VPN-доступ для {{ name }} к серверу {{ server_name }}.</p>

  <p>Ключ для подключения:</p>
  <div style="margin:12px 0;padding:12px 14px;border:1px solid #d6dbe3;border-radius:10px;background:#f7f9fc;word-break:break-all;">
    <span style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:13px;white-space:normal;">{{ vpn_link }}</span>
  </div>

  <p>Актуальные ссылки для скачивания:</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px;">
    <a
      href="https://amnezia.org/downloads"
      style="display:inline-block;padding:4px 10px;border:1px solid #d6dbe3;border-radius:999px;background:#f7f9fc;color:#1f6feb;text-decoration:none;font-size:12px;line-height:1.2;"
    >
      Официальный сайт
    </a>
    <a
      href="https://storage.googleapis.com/amnezia/amnezia.org"
      style="display:inline-block;padding:4px 10px;border:1px solid #d6dbe3;border-radius:999px;background:#f7f9fc;color:#1f6feb;text-decoration:none;font-size:12px;line-height:1.2;"
    >
      Зеркало
    </a>
  </div>

  <p>Не передавайте эту ссылку третьим лицам. Если нужен новый ключ, просто ответьте на это письмо.</p>
</div>
"""


def render_email_templates(
    *,
    to: str,
    vpn_link: str,
    client_name: str,
    server_name: str | None = None,
    subject_template: str = DEFAULT_EMAIL_SUBJECT_TEMPLATE,
    body_template: str = DEFAULT_EMAIL_BODY_TEMPLATE,
) -> tuple[str, str, str]:
    context: dict[str, Any] = {
        "name": client_name,
        "email": to,
        "vpn_link": vpn_link,
        "server_name": server_name or os.getenv("SERVER_NAME", "AWG Server"),
    }
    subject = Template(subject_template or DEFAULT_EMAIL_SUBJECT_TEMPLATE).render(**context).strip()
    body_text = Template(DEFAULT_EMAIL_TEXT_TEMPLATE).render(**context).strip()
    body_html = Template(body_template or DEFAULT_EMAIL_BODY_TEMPLATE).render(**context).strip()
    return subject, body_text, body_html


def send_email(
    *,
    to: str,
    vpn_link: str,
    client_name: str,
    server_name: str | None = None,
    subject_template: str = DEFAULT_EMAIL_SUBJECT_TEMPLATE,
    body_template: str = DEFAULT_EMAIL_BODY_TEMPLATE,
) -> Any:
    api_key = os.getenv("RESEND_API_KEY") or ""
    sender = os.getenv("EMAIL") or ""
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    if not sender:
        raise RuntimeError("EMAIL is not set")

    import resend
    from resend import Emails

    resend.api_key = api_key
    subject, body_text, body_html = render_email_templates(
        to=to,
        vpn_link=vpn_link,
        client_name=client_name,
        server_name=server_name,
        subject_template=subject_template,
        body_template=body_template,
    )
    params = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "text": body_text,
        "html": body_html,
    }
    return Emails.send(params)
