#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — WhatsApp рассылка тёплым лидам
#
#  Читает leads.csv (от parse_2gis.py), шлёт ПЕРСОНАЛИЗИРОВАННОЕ
#  первое сообщение каждому бизнесу через твой WhatsApp Web.
#  Цель — начать диалог и вывести на ЛПР, дальше общаешься сам.
#
#  ⚠️ ЧЕСТНО О РИСКЕ БАНА (читай обязательно):
#  WhatsApp банит номера за массовую холодную рассылку. Чтобы выжить:
#    • Используй ОТДЕЛЬНЫЙ номер (не личный!), желательно с историей.
#    • Не больше ~30-40 сообщений в день с нового номера (DAILY_LIMIT).
#    • Большие случайные паузы между сообщениями (уже встроено).
#    • Сообщения РАЗНЫЕ (шаблон рандомизирован — не один и тот же текст).
#    • Первым сообщением НЕ кидай ссылку — это триггер спам-фильтра.
#    • Отвечай на ответы ЛИЧНО и быстро — это снижает жалобы.
#  Это инструмент для холодного B2B-аутрича, не для спама физлиц.
#
#  УСТАНОВКА:
#    pip install -r requirements-marketing.txt
#    # нужен Google Chrome + chromedriver (selenium сам подтянет через
#    # selenium-manager в свежих версиях selenium 4.x)
#
#  ЗАПУСК:
#    python whatsapp_outreach.py            # боевой режим
#    python whatsapp_outreach.py --dry-run  # ничего не шлёт, только показывает
#    python whatsapp_outreach.py --limit 20
#
#  При первом запуске откроется Chrome → отсканируй QR в WhatsApp
#  (Настройки → Связанные устройства). Сессия сохранится в ./wa_profile.
# ════════════════════════════════════════════════════════════

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
import urllib.parse
from pathlib import Path

LEADS_FILE = Path(__file__).parent / "leads.csv"
SENT_FILE = Path(__file__).parent / "sent.csv"      # лог отправленных (не дублируем)
PROFILE_DIR = Path(__file__).parent / "wa_profile"  # сессия WhatsApp (QR один раз)

DAILY_LIMIT = 35          # максимум сообщений за запуск (защита от бана)
MIN_DELAY = 120           # минимальная пауза между сообщениями, сек (2 мин)
MAX_DELAY = 180           # максимальная пауза, сек (3 мин) — шлём по очереди, не разом

# Несколько вариантов первого сообщения — рандомизация снижает спам-метку.
# {name} подставляется. Без ссылок в первом касании!
MESSAGE_TEMPLATES = [
    "Здравствуйте! Это {name}? 😊 Меня зовут Данил, я делаю сервис для "
    "контроля качества обслуживания на кассе для кафе и магазинов в Казахстане. "
    "Помогает владельцу видеть как сотрудники общаются с клиентами и не теряются "
    "ли деньги. Можно показать как это работает у вас? Это бесплатно на первый месяц.",

    "Добрый день! Пишу владельцу/управляющему {name}. Я разработал сервис, "
    "который слушает разговоры на кассе и показывает: поздоровался ли кассир, "
    "предложил ли допродажу, не грубил ли, не уводит ли деньги мимо кассы. "
    "Хочу дать вам попробовать бесплатно на месяц. С кем можно обсудить?",

    "Сәлеметсіз бе! {name} иесімен/басқарушысымен сөйлесе аламын ба? Мен кассадағы "
    "қызмет сапасын бақылайтын сервис жасадым — сатушы сәлемдесті ме, дөрекілік "
    "болды ма, ақша кассадан тыс кетпеді ме. Бір ай тегін көрсетейін бе?",
]


def _load_sent() -> set[str]:
    if not SENT_FILE.exists():
        return set()
    with open(SENT_FILE, encoding="utf-8") as f:
        return {row["phone"] for row in csv.DictReader(f) if row.get("phone")}


def _log_sent(phone: str, name: str):
    new = not SENT_FILE.exists()
    with open(SENT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["phone", "name", "ts"])
        w.writerow([phone, name, time.strftime("%Y-%m-%d %H:%M:%S")])


def _load_leads() -> list[dict]:
    if not LEADS_FILE.exists():
        print(f"❌ Нет {LEADS_FILE}. Сначала запусти: python parse_2gis.py")
        sys.exit(1)
    with open(LEADS_FILE, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("phone")]


def main():
    ap = argparse.ArgumentParser(description="WhatsApp рассылка лидам TrustControl")
    ap.add_argument("--limit", type=int, default=DAILY_LIMIT,
                    help=f"Сколько сообщений за запуск (по умолч. {DAILY_LIMIT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Не отправлять — показать кому и что ушло бы")
    args = ap.parse_args()

    leads = _load_leads()
    sent = _load_sent()
    todo = [l for l in leads if l["phone"] not in sent][: args.limit]

    print(f"📋 Лидов всего: {len(leads)} | уже писали: {len(sent)} | "
          f"к отправке сейчас: {len(todo)}")

    if args.dry_run:
        for l in todo:
            msg = random.choice(MESSAGE_TEMPLATES).format(name=l["name"])
            print(f"\n— {l['name']} ({l['phone']})\n  {msg[:120]}…")
        print(f"\n(dry-run: ничего не отправлено)")
        return

    if not todo:
        print("✅ Все лиды уже обработаны. Запусти parse_2gis.py для новых.")
        return

    # Импортируем selenium только в боевом режиме (dry-run работает без него)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        print("❌ Нет selenium. Установи: pip install -r requirements-marketing.txt")
        sys.exit(1)

    opts = Options()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")   # сохраняем сессию
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=opts)
    driver.get("https://web.whatsapp.com")

    print("\n📱 Если открылся QR — отсканируй его в WhatsApp на телефоне:")
    print("   Настройки → Связанные устройства → Привязать устройство.")
    input("   После входа в WhatsApp нажми ENTER здесь...")

    ok, fail = 0, 0
    for i, lead in enumerate(todo, 1):
        phone = lead["phone"]
        name = lead["name"]
        msg = random.choice(MESSAGE_TEMPLATES).format(name=name)
        url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(msg)}"

        try:
            driver.get(url)
            # Ждём поле ввода (значит чат открылся и номер есть в WhatsApp)
            box = WebDriverWait(driver, 25).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
                )
            )
            time.sleep(random.uniform(2, 4))
            box.send_keys(Keys.ENTER)
            time.sleep(random.uniform(2, 3))
            _log_sent(phone, name)
            ok += 1
            print(f"[{i}/{len(todo)}] ✓ {name} ({phone})")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(todo)}] ✗ {name} ({phone}) — {type(e).__name__} "
                  f"(номера нет в WhatsApp или не загрузилось)")

        if i < len(todo):
            pause = random.uniform(MIN_DELAY, MAX_DELAY)
            print(f"      ⏳ пауза {pause:.0f}с (защита от бана)…")
            time.sleep(pause)

    print(f"\n✅ Отправлено: {ok} | не доставлено: {fail}")
    print(f"   Лог: {SENT_FILE}")
    print("   ⚠ Завтра запусти снова — продолжит с того места (дневной лимит).")
    driver.quit()


if __name__ == "__main__":
    main()
