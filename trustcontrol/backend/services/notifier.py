import logging
import asyncio
from datetime import datetime
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from backend.config import settings

log = logging.getLogger("notifier")
_bot: Bot | None = None

# Диагностика для дашборда: что в последний раз отвалилось у Telegram-бота.
# Юзер видит это в /api/auth/me и может починить токен / разблокировать бота.
last_telegram_error: dict = {"at": None, "msg": None, "chat_id": None}


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
        _bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _bot


async def _send(chat_id: str, text: str, reply_markup=None) -> bool:
    """Отправляет сообщение. True/False — успех. При сбое пишет в last_telegram_error."""
    try:
        await get_bot().send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        msg = str(e)[:200]
        log.error(f"Telegram ({chat_id}): {msg}")
        print(f"❌ TELEGRAM_FAIL chat={chat_id}: {msg}", flush=True)
        last_telegram_error["at"]      = datetime.utcnow().isoformat()
        last_telegram_error["msg"]     = msg
        last_telegram_error["chat_id"] = str(chat_id)
        return False


def _listen_button(audio_url: str | None) -> InlineKeyboardMarkup | None:
    if not audio_url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Слушать запись", url=audio_url)
    ]])


async def send_report(
    chat_id: str,
    location_name: str,
    transcript: str,
    found: dict,
    tone: str,
    score: float,
    audio_url: str | None = None,
):
    ts = datetime.now().strftime("%d.%m, %H:%M")

    if "🚨 МОШЕННИЧЕСТВО" in found:
        header = f"🚨 *Подозрение на кражу — {location_name}*"
        hits   = found["🚨 МОШЕННИЧЕСТВО"]
        detail = f"_{', '.join(hits[:2])}_"
    elif "⚠️ Грубость" in found:
        header = f"⚠️ *Грубость с клиентом — {location_name}*"
        hits   = found["⚠️ Грубость"]
        detail = f"_{', '.join(hits[:2])}_"
    else:
        header = f"⚠️ *Нарушение — {location_name}*"
        detail = ""

    lines = [header, f"_{ts}_"]
    if detail:
        lines += ["", detail]

    await _send(chat_id, "\n".join(lines), reply_markup=_listen_button(audio_url))


async def send_critical_alert(data: dict):
    chat_id = data.get("telegram_chat")
    if not chat_id:
        return

    summary       = data.get("summary", "—")
    audio_url     = data.get("audio_url") or ""
    location_name = data.get("location_name", "—")
    ts = datetime.now().strftime("%d.%m, %H:%M")

    text = (
        f"⚠️ *{location_name} — требует внимания*\n"
        f"_{ts}_\n\n"
        f"{summary}"
    )
    await _send(chat_id, text, reply_markup=_listen_button(audio_url) if audio_url else None)


async def send_incident_alert(
    chat_id: str,
    location_name: str,
    incident_type: str,
    description: str,
    incident_id: int | None = None,
    proof_s3_url: str | None = None,
    detected_phone: str | None = None,
    tx_amount: float | None = None,
    tx_receipt_id: str | None = None,
    tx_items: list | None = None,
):
    if not chat_id:
        return

    _TYPE_LABELS = {
        "KASPI_FRAUD": "🚨 Подозрение на кражу",
        "FRAUD":       "🚨 Кассовый разрыв",
        "AGGRESSION":  "⚠️ Грубость / конфликт",
        "UPSELL_GAP":  "Допродажа не предложена",
    }
    title = _TYPE_LABELS.get(incident_type, f"⚠️ Инцидент: {incident_type}")
    ts    = datetime.now().strftime("%d.%m.%Y %H:%M")

    lines = [
        f"*{title}*",
        f"*{location_name}* · {ts}",
        f"",
        f"{description}",
    ]

    if detected_phone:
        lines += [f"", f"Номер: `{detected_phone}`", "_Не числится в белом списке_"]

    if tx_amount is not None:
        lines += [f"", f"*Данные чека:*", f"Сумма: `{tx_amount:,.0f} ₸`"]
        if tx_receipt_id:
            lines.append(f"Чек №: `{tx_receipt_id}`")
        if tx_items:
            lines.append("Позиции:")
            for item in (tx_items or [])[:5]:
                name  = item.get("name", "?")
                qty   = item.get("qty") or item.get("quantity") or 1
                price = item.get("price") or item.get("sum") or "—"
                lines.append(f"  {name} × {qty} = {price} ₸")

    row1 = [InlineKeyboardButton("Прослушать запись", url=proof_s3_url)] if proof_s3_url else []
    row2 = []
    if incident_id:
        row2.append(InlineKeyboardButton("✅ Подтвердить", callback_data=f"tc_confirm:{incident_id}"))
        row2.append(InlineKeyboardButton("❌ Ошибка",      callback_data=f"tc_fp:{incident_id}"))

    all_rows = [r for r in [row1, row2] if r]
    markup = InlineKeyboardMarkup(all_rows) if all_rows else None
    await _send(chat_id, "\n".join(lines), reply_markup=markup)


async def send_daily_summary(chat_id: str, location_name: str, stats: dict):
    total = stats.get("total", 0)
    if total == 0:
        return

    upsell_pct   = stats.get("upsell_pct", 0)
    avg_sat      = stats.get("avg_satisfaction", 0.0)
    fraud_risks  = stats.get("fraud_risks", 0)
    negative     = stats.get("negative_count", 0)
    greeting_pct = stats.get("greeting_pct", 0)

    score_icon = "🟢" if avg_sat >= 4 else "🟡" if avg_sat >= 3 else "🔴"

    lines = [
        f"*Итог дня — {location_name}*",
        f"_{datetime.now().strftime('%d.%m.%Y')}_",
        f"",
        f"Разговоров: *{total}*",
        f"Приветствия: *{greeting_pct:.0f}%*",
        f"Допродажи: *{upsell_pct:.0f}%*",
        f"{score_icon} Оценка: *{avg_sat:.1f}/5*",
    ]

    if fraud_risks:
        lines.append(f"🚨 Подозрений на кражу: *{fraud_risks}*")
    if negative:
        lines.append(f"⚠️ Негативных разговоров: *{negative}*")

    lines.append(f"")
    lines.append(f"_Подробнее — в дашборде_")

    await _send(chat_id, "\n".join(lines))


async def send_ok_report(
    chat_id: str,
    location_name: str,
    transcript: str,
    tone: str,
    score: float,
    upsell: bool,
    greeting: bool,
    audio_url: str | None = None,
):
    score_icon = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
    tone_ru = {"positive": "позитивный", "neutral": "нейтральный", "negative": "негативный"}.get(tone, "нейтральный")

    flags = []
    if not greeting:
        flags.append("нет приветствия")
    if not upsell:
        flags.append("нет допродажи")
    flags_line = f"\n_{', '.join(flags)}_" if flags else ""

    # Показываем транскрипт только если он похож на настоящий разговор.
    # Мусор (2 слова на 60с, галлюцинация-повтор) владельцу не показываем.
    def _transcript_looks_ok(t: str) -> bool:
        if not t or not t.strip():
            return False
        words = t.split()
        if len(words) < 3:
            return False
        # повторяющаяся галлюцинация: много токенов, мало уникальных
        if len(words) >= 6 and len({w.lower() for w in words}) <= 2:
            return False
        return True

    transcript_line = f"\n_{transcript[:300]}_" if _transcript_looks_ok(transcript) else ""
    text = (
        f"{score_icon} *{location_name}* — {score:.0f}/100\n"
        f"Тон: {tone_ru}"
        f"{flags_line}"
        f"{transcript_line}"
    )
    await _send(chat_id, text, reply_markup=_listen_button(audio_url) if audio_url else None)


async def send_shift_summary(chat_id: str, location_name: str, shift_data: dict):
    s     = shift_data
    total = s.get("total_conversations", 1) or 1

    def pct(val):
        return round((val or 0) / total * 100)

    score = s.get("score", 0)
    score_icon = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"

    lines = [
        f"*Итог смены — {location_name}*",
        f"",
        f"Разговоров: *{total}*",
        f"Приветствия: *{s.get('greetings_count', 0)}* ({pct(s.get('greetings_count', 0))}%)",
        f"Благодарности: *{s.get('thanks_count', 0)}* ({pct(s.get('thanks_count', 0))}%)",
        f"Прощания: *{s.get('goodbye_count', 0)}* ({pct(s.get('goodbye_count', 0))}%)",
        f"Допродажи: *{s.get('bonus_count', 0)}* ({pct(s.get('bonus_count', 0))}%)",
        f"",
        f"Позитивный тон: *{s.get('positive_tone_count', 0)}*",
        f"Негативный тон: *{s.get('negative_tone_count', 0)}*",
    ]

    if s.get("bad_count", 0):
        lines.append(f"⚠️ Грубость: *{s['bad_count']}* раз")
    if s.get("fraud_count", 0):
        lines.append(f"🚨 Мошенничество: *{s['fraud_count']}* раз")

    lines += [f"", f"{score_icon} *Оценка смены: {score:.0f}/100*"]
    await _send(chat_id, "\n".join(lines))


async def send_fraud_email(
    user_email: str,
    location_name: str,
    incident_type: str,
    description: str,
    audio_url: str | None = None,
):
    """Sends email alert for fraud incidents via Resend API or SMTP fallback."""
    if not user_email:
        return

    from backend.config import settings

    subject = f"🚨 TrustControl: подозрение на мошенничество — {location_name}"
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    _TYPE_LABELS = {
        "KASPI_FRAUD": "Подозрение на кражу (Kaspi)",
        "FRAUD":       "Кассовый разрыв",
    }
    title = _TYPE_LABELS.get(incident_type, f"Инцидент: {incident_type}")

    body_html = f"""
<html><body style="font-family:sans-serif;max-width:600px">
<h2 style="color:#dc2626">🚨 {title}</h2>
<p><strong>Точка:</strong> {location_name}<br>
<strong>Время:</strong> {ts}</p>
<p>{description}</p>
{"<p><a href='" + audio_url + "'>▶ Прослушать запись</a></p>" if audio_url else ""}
<hr>
<p style="color:#6b7280;font-size:12px">TrustControl — AI-мониторинг качества обслуживания</p>
</body></html>
"""

    # Try Resend API first (works reliably on cloud hosting)
    if settings.RESEND_API_KEY:
        try:
            import httpx
            async with httpx.AsyncClient() as hclient:
                resp = await hclient.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                    json={
                        "from": settings.SMTP_FROM or "noreply@trustcontrol.kz",
                        "to": [user_email],
                        "subject": subject,
                        "html": body_html,
                    },
                    timeout=10,
                )
            if resp.status_code in (200, 201):
                log.info(f"Fraud email sent via Resend to {user_email}")
                return
            log.warning(f"Resend failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Resend email error: {e}")

    # Fallback: SMTP
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS):
        log.info("Email не настроен (нет RESEND_API_KEY или SMTP). Пропускаем.")
        return

    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        import asyncio

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"]      = user_email
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        def _smtp_send():
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as srv:
                srv.starttls()
                srv.login(settings.SMTP_USER, settings.SMTP_PASS)
                srv.send_message(msg)

        await asyncio.get_event_loop().run_in_executor(None, _smtp_send)
        log.info(f"Fraud email sent via SMTP to {user_email}")
    except Exception as e:
        log.error(f"SMTP fraud email failed: {e}")
