import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("email_sender")


async def send_otp_email(email: str, code: str, name: str = "") -> bool:
    from backend.config import settings

    if not settings.SMTP_HOST or not settings.SMTP_USER:
        log.warning(f"[DEV] OTP для {email}: {code}  (SMTP не настроен)")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"TrustControl — код подтверждения: {code}"
    msg["From"]    = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"]      = email

    greeting = f"Привет, {name}!" if name else "Привет!"
    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Inter',sans-serif">
  <div style="max-width:420px;margin:40px auto;padding:32px;
              background:#1e1e1e;border-radius:16px;
              border:1px solid rgba(255,255,255,.08)">
    <div style="font-size:22px;font-weight:700;margin-bottom:4px">
      <span style="color:#00e676">Trust</span><span style="color:#fff">Control</span>
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

    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        import aiosmtplib
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASS,
            start_tls=True,
        )
        log.info(f"OTP отправлен на {email}")
        return True
    except Exception as exc:
        log.error(f"Не удалось отправить OTP на {email}: {exc}")
        return False
