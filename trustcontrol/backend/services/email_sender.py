import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("email_sender")


def _build_html(code: str, name: str) -> str:
    greeting = f"Привет, {name}!" if name else "Привет!"
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Inter',sans-serif">
  <div style="max-width:420px;margin:40px auto;padding:32px;
              background:#1e1e1e;border-radius:16px;
              border:1px solid rgba(255,255,255,.08)">
    <div style="font-size:22px;font-weight:700;margin-bottom:4px">
      <span style="color:#c9a84c">Trust</span><span style="color:#fff">Control</span>
    </div>
    <p style="color:#aaa;font-size:14px;margin-top:0">{greeting}</p>
    <p style="color:#f5f5f5;font-size:15px">Ваш код подтверждения:</p>
    <div style="background:#121212;border-radius:12px;padding:24px 0;
                text-align:center;font-size:40px;font-weight:700;
                letter-spacing:16px;color:#fff;margin:20px 0">
      {code}
    </div>
    <p style="color:#666;font-size:13px;margin-bottom:0">
      Код действителен <b style="color:#aaa">10 минут</b>.
      Никому его не передавайте.
    </p>
  </div>
</body>
</html>"""


async def send_otp_email(email: str, code: str, name: str = "") -> bool:
    from backend.config import settings

    subject = f"TrustControl — код подтверждения: {code}"
    html    = _build_html(code, name)

    # ── Resend HTTP API (рекомендуется на Render) ─────────────
    if settings.RESEND_API_KEY:
        return await _send_via_resend(
            api_key  = settings.RESEND_API_KEY,
            from_addr= settings.SMTP_FROM or "TrustControl <onboarding@resend.dev>",
            to_email = email,
            subject  = subject,
            html     = html,
        )

    # ── SMTP fallback ──────────────────────────────────────────
    if settings.SMTP_HOST and settings.SMTP_USER:
        return await _send_via_smtp(
            to_email = email,
            subject  = subject,
            html     = html,
        )

    # ── Dev fallback — печатаем в лог ─────────────────────────
    log.warning(f"[DEV] OTP для {email}: {code}  (ни RESEND_API_KEY, ни SMTP не настроены)")
    return True


async def _send_via_resend(api_key: str, from_addr: str, to_email: str,
                           subject: str, html: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "from":    from_addr,
                    "to":      [to_email],
                    "subject": subject,
                    "html":    html,
                },
            )
        if r.status_code in (200, 201):
            log.info(f"OTP отправлен через Resend на {to_email}")
            return True
        log.error(f"Resend вернул {r.status_code}: {r.text}")
        return False
    except Exception as exc:
        log.error(f"Resend ошибка: {exc}")
        return False


async def _send_via_smtp(to_email: str, subject: str, html: str) -> bool:
    from backend.config import settings

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        import aiosmtplib
        kwargs = dict(
            hostname = settings.SMTP_HOST,
            port     = settings.SMTP_PORT,
            username = settings.SMTP_USER,
            password = settings.SMTP_PASS,
        )
        # Порт 465 = SSL, всё остальное = STARTTLS
        if settings.SMTP_PORT == 465:
            kwargs["use_tls"] = True
        else:
            kwargs["start_tls"] = True

        await aiosmtplib.send(msg, **kwargs)
        log.info(f"OTP отправлен через SMTP на {to_email}")
        return True
    except Exception as exc:
        log.error(f"SMTP ошибка: {exc}")
        return False
