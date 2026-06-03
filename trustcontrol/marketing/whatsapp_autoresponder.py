#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — AI-продавец (SDR) в WhatsApp
#
#  Читает входящие ответы от бизнесов (на номерах из 2ГИС), сам
#  отвечает через GPT как ассистент Данила, ведёт диалог к одной цели:
#  выйти на ЛПР и забить ВРЕМЯ СОЗВОНА. Когда лид «тёплый» — пишет
#  в hot_leads.csv и пингует тебя в Telegram.
#
#  ⚠️ Это дополнение к scripts/whatsapp_sender.py:
#    1. whatsapp_sender.py — отправил первые сообщения
#    2. autoresponder.py — крутится в фоне и отвечает на ответы
#  Оба используют ОДНУ сессию WhatsApp (marketing/wa_session/) → QR один раз.
#
#  СТОИМОСТЬ: ~$0.0004 за сообщение (gpt-4o-mini). 500 лидов ≈ $1.2.
#
#  ⚠️ РИСК: бот в WhatsApp Web через Selenium — серая зона, номер
#  могут забанить. Отвечать тем, кто ОТВЕТИЛ сам — безопаснее, чем
#  холодная рассылка (это сигнал вовлечённости), но не злоупотребляй.
#
#  ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ:
#    OPENAI_API_KEY=sk-...           # обязательно
#    TELEGRAM_BOT_TOKEN=...          # опц.: пинг о горячем лиде
#    TELEGRAM_ADMIN_CHAT_ID=...      # опц.: твой chat_id
#
#  ЗАПУСК (на сервере, в фоне):
#    python whatsapp_autoresponder.py
#    python whatsapp_autoresponder.py --poll 30   # проверять каждые 30с
# ════════════════════════════════════════════════════════════

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
# Общая папка с рассыльщиком (scripts/whatsapp_sender.py): ОДНА сессия WhatsApp
# на оба скрипта → QR сканируем один раз. Путь не зависит от cwd.
_WA_DIR = HERE / "wa_session"
_WA_DIR.mkdir(parents=True, exist_ok=True)
CONV_FILE = _WA_DIR / "conversations.json"  # история переписок {phone: [msgs]}
HOT_FILE = _WA_DIR / "hot_leads.csv"        # горячие лиды с временем созвона
PROFILE_DIR = _WA_DIR / "wa_profile"        # та же сессия что у рассыльщика
SENT_LOG = _WA_DIR / "wa_sent.log"          # кому писал рассыльщик (белый список)

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# ── О продукте — бот отвечает на основе этого. Правь под себя. ──
PRODUCT_BRIEF = """
TrustControl — сервис ИИ-контроля качества обслуживания на кассе.
Как работает: микрофон на кассе → ИИ слушает разговоры → показывает владельцу
в Telegram и на дашборде: поздоровался ли кассир, предложил ли допродажу,
грубил ли, доволен ли клиент, не уводят ли деньги мимо кассы (фрод).
Понимает казахский, русский и шала-казахский.
Для кого: кофейни, аптеки, фастфуд, магазины, АЗС в Казахстане.
7 ДНЕЙ БЕСПЛАТНО, без карты и обязательств. Цена потом — от 19 990 ₸/мес за точку.
Окупается одной пойманной утечкой (одна упущенная допродажа в день ≈ 120 000 ₸/мес
мимо кассы). Установка простая: программа ставится на обычный ПК у кассы (сам кассовый
аппарат не трогаем). Нужен USB-микрофон, покупает клиент сам: для тихого помещения —
простой от 3 000 ₸, для шумного зала — направленный кардиоидный (~12 000 ₸), поможем
подобрать под помещение. Владелец: Данил.
"""

SYSTEM_PROMPT = f"""Ты — вежливый деловой ассистент Данила, основателя TrustControl.
Ты ведёшь переписку в WhatsApp с владельцами/менеджерами кафе, аптек и фастфудов
в Казахстане, которым мы написали первыми. На том конце часто сидит менеджер или
администратор, а не сам владелец (ЛПР — лицо принимающее решение).

О ПРОДУКТЕ:
{PRODUCT_BRIEF}

ТВОЯ ЕДИНСТВЕННАЯ ЦЕЛЬ: договориться о коротком созвоне Данила с ЛПР (владельцем
или управляющим). Не продавай в переписке — продаёт Данил на созвоне.

КАК ВЕСТИ ДИАЛОГ:
1. Отвечай коротко, по-человечески, тепло. Можно на казахском если пишут на казахском.
2. Кратко объясни ценность если спрашивают «что это/зачем». Не вали простынёй.
3. На вопрос о цене: «7 дней бесплатно, без карты. Дальше — от 19 990 ₸/мес за точку,
   окупается одной пойманной утечкой. Точный тариф под ваш формат Данил подберёт на
   созвоне» — можно назвать «от 15 000 ₸», но НЕ выдумывай других сумм и не торгуйся.
4. Если пишет НЕ ЛПР — вежливо узнай, можно ли связаться с владельцем/управляющим
   и когда удобно.
5. Если интерес есть — предложи созвон и СПРОСИ удобные день и время.
6. Если отказ/не интересно — поблагодари, не дави, заверши вежливо.

НА ВЫХОДЕ всегда верни ТОЛЬКО валидный JSON:
{{
  "reply": "твой ответ клиенту (то что отправим в WhatsApp)",
  "stage": "intro|qualifying|booking|booked|declined",
  "interested": true|false|null,
  "is_decision_maker": true|false|null,
  "proposed_call_time": "распознанное время созвона текстом, или пусто",
  "notify_owner": true|false
}}
notify_owner=true ТОЛЬКО когда есть конкретная договорённость о созвоне
(stage=booked) или клиент явно просит чтобы Данил позвонил/написал лично.
"""


# ── Белый список: отвечаем ТОЛЬКО тем, кому писал рассыльщик ──────────────────
# КРИТИЧНО: без этого бот ответил бы рекламой на ВСЕ непрочитанные чаты, включая
# твоих реальных клиентов/родных/группы. Отвечаем только на номера из wa_sent.log.

def normalize_phone(raw: str) -> str | None:
    """+7 707 123 45 67 / 8 (707)... → 7707xxxxxxx. None если это не номер
    (например имя сохранённого контакта или название группы)."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return digits
    if len(digits) == 10:
        return "7" + digits
    return None


def load_allowed_phones() -> set[str]:
    """Номера, которым писал рассыльщик (wa_sent.log). Формат строки:
    7707xxxxxxx<TAB>имя<TAB>дата."""
    if not SENT_LOG.exists():
        return set()
    allowed = set()
    for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        p = normalize_phone(line.split("\t")[0])
        if p:
            allowed.add(p)
    return allowed


def _load_conv() -> dict:
    if CONV_FILE.exists():
        return json.loads(CONV_FILE.read_text(encoding="utf-8"))
    return {}


def _save_conv(conv: dict):
    CONV_FILE.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_hot(phone: str, name: str, time_str: str, summary: str):
    new = not HOT_FILE.exists()
    with open(HOT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["phone", "name", "proposed_time", "summary", "ts"])
        w.writerow([phone, name, time_str, summary, time.strftime("%Y-%m-%d %H:%M")])


def _notify_telegram(text: str):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def gpt_reply(history: list[dict]) -> dict:
    """История [{role, content}] → JSON-ответ бота."""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
        response_format={"type": "json_object"},
        max_tokens=400,
        temperature=0.5,
    )
    return json.loads(resp.choices[0].message.content)


# ════════════════════════════════════════════════════════════
#  WhatsApp Web через Selenium
#  ⚠️ Селекторы WhatsApp периодически меняются — если перестало
#     читать чаты, чинить надо здесь (_get_unread / _read_incoming).
# ════════════════════════════════════════════════════════════

def _make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=opts)
    driver.get("https://web.whatsapp.com")
    return driver


def _get_unread_chats(driver):
    """Возвращает элементы чатов с непрочитанными сообщениями.
    Бейдж непрочитанных помечен aria-label — на ru «N непрочитан…»,
    на en «N unread message…». Ловим оба языка интерфейса."""
    from selenium.webdriver.common.by import By
    chats, seen = [], set()
    badges = (
        driver.find_elements(By.XPATH, '//span[contains(@aria-label, "непрочит")]')
        + driver.find_elements(By.XPATH, '//span[contains(@aria-label, "unread")]')
    )
    for badge in badges:
        try:
            row = badge.find_element(By.XPATH, './ancestor::div[@role="listitem"]')
            if row.id not in seen:          # один чат не добавляем дважды
                seen.add(row.id)
                chats.append(row)
        except Exception:
            continue
    return chats


def _read_incoming(driver) -> list[str]:
    """Читает последние ВХОДЯЩИЕ сообщения в открытом чате."""
    from selenium.webdriver.common.by import By
    msgs = []
    for el in driver.find_elements(By.XPATH, '//div[contains(@class,"message-in")]'):
        txt = el.text.strip()
        if txt:
            msgs.append(txt)
    return msgs[-5:]  # последние 5 входящих — контекст


def _find_compose_box(driver):
    """Поле ввода сообщения. data-tab меняется между версиями WhatsApp,
    поэтому пробуем несколько селекторов; поле всегда внизу (footer)."""
    from selenium.webdriver.common.by import By
    selectors = [
        '//footer//div[@contenteditable="true"]',
        '//div[@contenteditable="true"][@data-tab="10"]',
        '//div[@contenteditable="true"][@role="textbox"]',
    ]
    for sel in selectors:
        els = driver.find_elements(By.XPATH, sel)
        if els:
            return els[-1]      # последнее contenteditable = поле ввода, не поиск
    return None


def _send_message(driver, text: str) -> bool:
    from selenium.webdriver.common.keys import Keys
    box = _find_compose_box(driver)
    if box is None:
        return False
    box.click()
    # send_keys по строкам — WhatsApp не любит \n как Enter
    lines = text.split("\n")
    for i, line in enumerate(lines):
        box.send_keys(line)
        if i < len(lines) - 1:
            box.send_keys(Keys.SHIFT, Keys.ENTER)
    box.send_keys(Keys.ENTER)
    return True


def _chat_phone_name(driver) -> tuple[str, str]:
    """Достаёт заголовок открытого чата. Для несохранённого лида заголовок —
    это его НОМЕР (+7 ...), для сохранённого контакта — имя."""
    from selenium.webdriver.common.by import By
    for xp in ('//header//span[@dir="auto"]', '//header//span[@title]'):
        try:
            el = driver.find_element(By.XPATH, xp)
            title = (el.get_attribute("title") or el.text or "").strip()
            if title:
                return title, title
        except Exception:
            continue
    return "", ""


def run(poll_interval: int, reply_all: bool = False):
    if not OPENAI_KEY:
        print("❌ Нет OPENAI_API_KEY. export OPENAI_API_KEY=sk-...")
        return

    try:
        from selenium.webdriver.common.by import By
    except ImportError:
        print("❌ Нет selenium. pip install -r requirements-marketing.txt")
        return

    driver = _make_driver()
    print("\n📱 Если открылся QR — отсканируй в WhatsApp (Связанные устройства).")
    input("   После входа нажми ENTER...")

    conv = _load_conv()

    # Белый список: отвечаем ТОЛЬКО тем, кому писал рассыльщик. Защищает твои
    # реальные чаты (клиенты, родные, группы) от авто-рекламы.
    allowed = load_allowed_phones() if not reply_all else set()
    if reply_all:
        print("⚠️  РЕЖИМ reply-all: бот ответит на ЛЮБОЙ непрочитанный чат, "
              "включая твои реальные контакты. Используй осознанно.")
    elif not allowed:
        print("❌ wa_sent.log пуст — некому отвечать (рассыльщик ещё не слал, "
              "или сессия в другой папке). Чтобы не спамить реальные чаты, "
              "бот НЕ отвечает никому. Сначала запусти рассылку.")
        driver.quit()
        return
    else:
        print(f"✓ Белый список: {len(allowed)} номеров из рассылки. "
              "Только им бот и ответит.")

    print(f"🤖 AI-продавец запущен. Проверка каждые {poll_interval}с. Ctrl+C для выхода.")

    try:
        while True:
            unread = _get_unread_chats(driver)
            if unread:
                print(f"\n📨 Непрочитанных чатов: {len(unread)}")
            for chat in unread:
                try:
                    chat.click()
                    time.sleep(2)
                    name, phone = _chat_phone_name(driver)
                    if not name:
                        continue

                    # ── ФИЛЬТР: только наши лиды ──────────────────────────────
                    norm = normalize_phone(phone)
                    if not reply_all and (norm is None or norm not in allowed):
                        # Сохранённый контакт (имя вместо номера) или чужой чат —
                        # это НЕ лид из рассылки. Не трогаем.
                        print(f"  ⏭  пропуск (не наш лид): {name[:40]}")
                        continue
                    key = norm or name

                    incoming = _read_incoming(driver)
                    if not incoming:
                        continue
                    last = incoming[-1]

                    hist = conv.get(key, [])
                    hist.append({"role": "user", "content": last})

                    result = gpt_reply(hist)
                    reply = result.get("reply", "").strip()
                    if not reply:
                        continue

                    if not _send_message(driver, reply):
                        print(f"  ! поле ввода не найдено — пропуск {name[:30]}")
                        continue
                    hist.append({"role": "assistant", "content": reply})
                    conv[key] = hist[-20:]   # держим последние 20 реплик
                    _save_conv(conv)

                    print(f"  ↪ {name}: «{last[:50]}» → «{reply[:50]}» "
                          f"[{result.get('stage')}]")

                    if result.get("notify_owner"):
                        t = result.get("proposed_call_time", "")
                        _log_hot(phone, name, t, last[:120])
                        _notify_telegram(
                            f"🔥 Горячий лид: {name}\n"
                            f"Время созвона: {t or 'уточнить'}\n"
                            f"Последнее сообщение: {last[:200]}"
                        )
                        print(f"  🔥 ГОРЯЧИЙ ЛИД → hot_leads.csv + Telegram")

                    time.sleep(3)
                except Exception as e:
                    print(f"  ! чат пропущен: {type(e).__name__}: {e}")
                    continue

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n👋 Остановлено.")
    finally:
        driver.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI-продавец TrustControl в WhatsApp")
    ap.add_argument("--poll", type=int, default=30, help="Интервал проверки чатов, сек")
    ap.add_argument("--all-chats", action="store_true",
                    help="ОПАСНО: отвечать на ВСЕ чаты, не только лидам из рассылки. "
                         "По умолчанию бот отвечает только тем, кому писал рассыльщик.")
    args = ap.parse_args()
    run(args.poll, reply_all=args.all_chats)
