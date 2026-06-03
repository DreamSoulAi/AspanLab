#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Авто-рассылка лидам в WhatsApp
#
#  Берёт лиды из leadgen_2gis.py (leads_*.json) и шлёт каждому
#  ПЕРСОНАЛЬНОЕ сообщение под его боль через web.whatsapp.com.
#
#  ⚠️  ПРОЧТИ ПЕРЕД ЗАПУСКОМ — иначе потеряешь номер:
#    • Используй ОТДЕЛЬНЫЙ номер, НЕ основной рабочий. WhatsApp банит
#      за холодную рассылку. Если забанят — потеряешь все переписки.
#    • Холодная массовая рассылка нарушает правила WhatsApp. Риск на тебе.
#    • Скрипт намеренно медленный (паузы, дневной лимит) — НЕ убирай защиту,
#      она единственное что спасает номер от мгновенного бана.
#    • Номер в 2ГИС обычно МЕНЕДЖЕРА, не владельца. Цель сообщения —
#      зацепить болью так, чтобы переслали ЛПР. Поэтому текст персональный.
#
#  Защита от бана (встроена):
#    • дневной лимит (по умолчанию 20)
#    • случайные паузы 90–240 сек между сообщениями
#    • только рабочие часы
#    • разный текст каждому (одинаковый = бан)
#    • лог отправленных — один лид не получит двойное сообщение
#    • DRY-RUN по умолчанию: реально шлёт только с флагом --send
#
#  Запуск (на своём ПК, где есть Chrome и интернет):
#    pip install selenium webdriver-manager
#    python scripts/leadgen_2gis.py --niche coffee --analyze 50   # сначала лиды
#    python scripts/whatsapp_sender.py --leads leads_Алматы_coffee.json   # dry-run
#    python scripts/whatsapp_sender.py --leads leads_Алматы_coffee.json --send
#
#  Первый запуск: откроется Chrome → отсканируй QR в WhatsApp.
#  Сессия сохранится в ./wa_profile — больше QR сканировать не надо.
# ════════════════════════════════════════════════════════════

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

# Общая папка WhatsApp-инструментов: рассыльщик и авто-ответчик
# (marketing/whatsapp_autoresponder.py) смотрят в ОДНУ сессию → QR сканируем
# один раз. Путь абсолютный (от расположения файла), не зависит от cwd.
_WA_DIR = Path(__file__).resolve().parent.parent / "marketing" / "wa_session"
_WA_DIR.mkdir(parents=True, exist_ok=True)

SENT_LOG = str(_WA_DIR / "wa_sent.log")       # номера, которым уже писали
PROFILE_DIR = str(_WA_DIR / "wa_profile")     # сессия WhatsApp (QR один раз)

# Мусор по названию — заправки и сети, которым слать бессмысленно.
# 2ГИС иногда метит их рубрикой «кафе», поэтому фильтруем ещё и здесь.
JUNK_NAME_WORDS = [
    "lukoil", "лукойл", "qazaq oil", "казахойл", "qazaqoil",
    "газпром", "gazprom", "helios", "гелиос", "compass", "компас",
    "royal petrol", "azs", "азс", "wissol", "socar", "сокар",
    "shell", "шелл", "petrol", "петрол", "заправ",
]


def is_junk_lead(lead: dict) -> bool:
    name = (lead.get("name") or "").lower()
    return any(w in name for w in JUNK_NAME_WORDS)


# ── Телефоны ───────────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str | None:
    """+7 707 123 45 67 / 8 (707)... → 7707xxxxxxx. None если мусор."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return digits
    if len(digits) == 10:               # без кода страны
        return "7" + digits
    return None


# ── Лог отправленных ─────────────────────────────────────────────────────────────

def load_sent() -> set[str]:
    if not os.path.exists(SENT_LOG):
        return set()
    with open(SENT_LOG, encoding="utf-8") as f:
        return {line.split("\t")[0].strip() for line in f if line.strip()}


def mark_sent(phone: str, name: str):
    with open(SENT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{phone}\t{name}\t{datetime.now().isoformat(timespec='seconds')}\n")


# ── Текст сообщения ──────────────────────────────────────────────────────────────

# Варианты формулировок — чтобы КАЖДОЕ сообщение было уникальным.
# Одинаковый текст всем = мгновенный бан WhatsApp. Поэтому собираем
# сообщение из случайно выбранных кусочков (приветствие + боль + оффер + вопрос).

_GREETINGS = [
    "Здравствуйте! Пишу по «{name}».",
    "Добрый день! Вопрос по заведению «{name}».",
    "Здравствуйте, обращаюсь насчёт «{name}».",
    "Приветствую! Пишу по «{name}».",
    "Добрый день! Нашёл вас в 2ГИС — «{name}».",
]

_BODY_LOWRATING = [
    "Заметил в отзывах замечания к обслуживанию на кассе — некорректное общение с гостями, "
    "ошибки при расчёте, долгое ожидание.",
    "Обратил внимание: в отзывах в 2ГИС встречаются жалобы на тон сотрудников и ошибки на кассе.",
    "Вижу в отзывах недовольство сервисом — встречается недоброжелательное общение и неточности в расчёте.",
    "В отзывах попадаются замечания к работе кассы: сухой тон сотрудников, очереди, ошибки со сдачей.",
]

_BODY_NEUTRAL = [
    "Занимаюсь контролем качества обслуживания на кассе.",
    "Помогаю владельцам кафе отслеживать качество работы сотрудников на кассе.",
    "Делаю так, чтобы владелец видел, как сотрудники общаются с гостями и насколько вовлечены.",
]

_OFFER = [
    ("Мы сделали ИИ-сервис TrustControl — он анализирует разговоры на кассе и отмечает "
     "тон и доброжелательность сотрудника, наличие приветствия и прощания, корректность расчёта "
     "и упущенные допродажи. Сводка владельцу в Telegram."),
    ("Сделали сервис TrustControl: ИИ слушает кассу и оценивает, насколько вежливо и вовлечённо "
     "общается сотрудник, поздоровался ли, предложил ли допродажу и верно ли провёл расчёт. "
     "Отчёт владельцу в Telegram."),
    ("У нас есть ИИ TrustControl — он анализирует общение на кассе: тональность и настрой сотрудника, "
     "приветствие, корректность расчёта и предложил ли он что-то к заказу (допродажа). "
     "Владелец видит понятный отчёт в Telegram."),
]

_ASK = [
    "Подскажите, с кем можно обсудить — это вопрос к управляющему или к собственнику?",
    "С кем лучше переговорить — с управляющим или напрямую с владельцем?",
    "Не подскажете, кому передать — управляющему или собственнику заведения?",
    "Это интересно решает управляющий или сам собственник? Хочу написать тому, кто решает.",
]


def build_message(lead: dict) -> str:
    """
    Персональное сообщение под конкретный лид.
    Если leadgen прогнал GPT — используем готовый pitch/pain.
    Иначе — собираем из случайных вариантов (каждое сообщение уникально → меньше риск бана).
    """
    name = lead.get("name", "")
    pain = (lead.get("pain") or "").strip()
    pitch = (lead.get("pitch") or "").strip()

    greeting = random.choice(_GREETINGS).format(name=name)

    if pitch:
        body = pitch
    elif pain:
        body = f"Обратил внимание: {pain}"
    else:
        rating = lead.get("rating")
        if rating and rating < 4.3:
            body = random.choice(_BODY_LOWRATING)
        else:
            body = random.choice(_BODY_NEUTRAL)

    offer = random.choice(_OFFER)
    ask = random.choice(_ASK)

    return f"{greeting} {body} {offer} {ask}"


# ── Рабочие часы ─────────────────────────────────────────────────────────────────

def within_business_hours(start_h: int, end_h: int) -> bool:
    h = datetime.now().hour
    return start_h <= h < end_h


# ── Selenium ─────────────────────────────────────────────────────────────────────

def make_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        sys.exit("Нужен selenium:  pip install selenium webdriver-manager")

    opts = Options()
    opts.add_argument(f"--user-data-dir={os.path.abspath(PROFILE_DIR)}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    try:
        return webdriver.Chrome(options=opts)
    except Exception as e:
        sys.exit(f"Не удалось запустить Chrome: {e}\n"
                 "Установи Chrome и: pip install webdriver-manager")


def wait_logged_in(driver):
    """Ждёт пока пользователь отсканирует QR (один раз)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get("https://web.whatsapp.com")
    print("\n>>> ОТСКАНИРУЙ QR-код в открывшемся окне Chrome:")
    print(">>> Телефон → WhatsApp → Настройки → Связанные устройства → Привязать устройство")
    print(">>> Жду авторизацию (до 5 минут)...\n")
    # 300с: на ПЕРВЫЙ вход с QR 120с мало (найти окно, открыть телефон, отсканировать).
    # После первого раза сессия в wa_session/ — вход мгновенный, ждать не придётся.
    WebDriverWait(driver, 300).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'div[contenteditable="true"]'))
    )
    print("✓ WhatsApp авторизован")


def send_one(driver, phone: str, message: str, timeout: int = 40) -> bool:
    """Отправляет одно сообщение через deep-link. True если ушло."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(message)}"
    driver.get(url)
    try:
        box = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'div[contenteditable="true"][data-tab="10"]')
            )
        )
        time.sleep(random.uniform(1.5, 3.0))
        box.send_keys(Keys.ENTER)
        time.sleep(random.uniform(2.0, 4.0))
        return True
    except Exception:
        # Чаще всего: номера нет в WhatsApp, или верстка изменилась
        return False


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Авто-рассылка лидам в WhatsApp")
    ap.add_argument("--leads", required=True, help="JSON от leadgen_2gis.py")
    ap.add_argument("--send", action="store_true",
                    help="РЕАЛЬНО отправлять. Без флага — dry-run (только показ).")
    ap.add_argument("--limit", type=int, default=20, help="дневной лимит (НЕ задирай)")
    ap.add_argument("--top", type=int, default=0, help="только топ-N по температуре (0=все)")
    ap.add_argument("--min-delay", type=int, default=90, help="мин пауза, сек")
    ap.add_argument("--max-delay", type=int, default=240, help="макс пауза, сек")
    ap.add_argument("--hours", default="10-19", help="рабочие часы, напр. 10-19")
    args = ap.parse_args()

    if not os.path.exists(args.leads):
        sys.exit(f"Файл не найден: {args.leads}")
    with open(args.leads, encoding="utf-8") as f:
        leads = json.load(f)

    # сразу выкидываем заправки и т.п., чтобы они не съедали места в топе
    before = len(leads)
    leads = [ld for ld in leads if not is_junk_lead(ld)]
    skipped_junk = before - len(leads)

    # сортировка по температуре, опц. срез топа
    leads.sort(key=lambda x: x.get("heat", 0), reverse=True)
    if args.top:
        leads = leads[:args.top]

    start_h, end_h = (int(x) for x in args.hours.split("-"))
    sent_before = load_sent()

    # отбираем кандидатов: есть телефон в WhatsApp-формате, ещё не писали
    queue = []
    for ld in leads:
        phone = normalize_phone(ld.get("phone", ""))
        if not phone:
            continue
        if phone in sent_before:
            continue
        queue.append((phone, ld))

    if args.limit:
        queue = queue[:args.limit]

    mode = "ОТПРАВКА" if args.send else "DRY-RUN (ничего не отправляется)"
    print(f"\n=== Режим: {mode} ===")
    if skipped_junk:
        print(f"Отфильтровано заправок/мусора: {skipped_junk}")
    print(f"Лидов в очереди: {len(queue)} (лимит {args.limit})")
    print(f"Паузы: {args.min_delay}-{args.max_delay} сек | Часы: {args.hours}\n")

    if not queue:
        print("Некому писать (нет валидных номеров или всем уже писали).")
        return

    # DRY-RUN: показать что ушло бы
    if not args.send:
        for phone, ld in queue:
            print(f"→ +{phone}  {ld.get('name','')}  (heat={ld.get('heat')})")
            print(f"   {build_message(ld)}\n")
        print("Это dry-run. Для реальной отправки добавь флаг --send")
        return

    # РЕАЛЬНАЯ ОТПРАВКА
    if not within_business_hours(start_h, end_h):
        print(f"Сейчас вне рабочих часов ({args.hours}). Отправка отменена "
              f"(чтобы не палиться ночными сообщениями).")
        return

    driver = make_driver()
    wait_logged_in(driver)

    ok = 0
    for i, (phone, ld) in enumerate(queue, 1):
        if not within_business_hours(start_h, end_h):
            print("Рабочие часы кончились — останавливаюсь.")
            break

        msg = build_message(ld)
        print(f"[{i}/{len(queue)}] +{phone} {ld.get('name','')} ... ", end="", flush=True)

        if send_one(driver, phone, msg):
            mark_sent(phone, ld.get("name", ""))
            ok += 1
            print("✓")
        else:
            print("✗ (нет в WhatsApp или ошибка)")

        if i < len(queue):
            pause = random.uniform(args.min_delay, args.max_delay)
            print(f"    пауза {pause:.0f} сек...")
            time.sleep(pause)

    print(f"\nГотово. Отправлено: {ok}/{len(queue)}")
    print("Закрываю браузер через 5 сек...")
    time.sleep(5)
    driver.quit()


if __name__ == "__main__":
    main()
