#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Автопостинг в Threads
#
#  Сам придумывает тему, пишет пост в стиле Данила и публикует в
#  Threads по расписанию. Темы НЕ нужно писать руками — бот берёт
#  угол из «банка углов» (наживки / боль / позиционирование), который
#  давно не использовался, и генерит свежий текст через GPT (без
#  повторов — помнит прошлые посты).
#
#  РЕЖИМЫ (env THREADS_MODE):
#    veto   — (по умолчанию) шлёт черновик тебе в Telegram с кнопками.
#             Не тронул за HOLD минут → публикует сам. 1 кнопка = контроль.
#    auto   — публикует сразу, без спроса (полный автопилот).
#    draft  — только генерит и шлёт в Telegram, НЕ публикует.
#
#  ЧАСТОТА: 1 пост/день, вечер (пик Threads). Для KZ — 20:00 Алматы.
#           Не чаще: алгоритм режет охваты у частящих новых аккаунтов.
#
#  ГДЕ КРУТИТЬ: VPS (всегда включён). Render free-tier засыпает — не годится.
#
#  ⚠️ ОТДЕЛЬНЫЙ Telegram-бот: вето работает через СВОЙ бот-токен
#     (THREADS_TG_TOKEN), а не прод-бот. Прод-бот сидит на webhook —
#     getUpdates на нём ломается. Заведи 2-й бот у @BotFather (2 мин).
#
#  ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ:
#    OPENAI_API_KEY=sk-...            # обязательно (генерация)
#    THREADS_USER_ID=...              # Threads user id (Meta app)
#    THREADS_ACCESS_TOKEN=...         # долгоживущий токен Threads API
#    THREADS_TG_TOKEN=...             # токен 2-го (контентного) бота — для вето
#    THREADS_TG_CHAT=...              # твой chat_id (куда слать черновик)
#    THREADS_MODE=veto                # veto | auto | draft
#    THREADS_HOLD_MIN=60              # сколько ждать вето перед автопубликацией
#
#  ЗАПУСК:
#    python threads_bot.py --dry          # сгенерить и показать, никуда не слать
#    python threads_bot.py --once         # один цикл (для cron в 19:00)
#    python threads_bot.py --loop         # демон: сам ждёт времени поста (systemd)
#
#  CRON (вариант без демона), каждый день 19:00 Алматы (14:00 UTC):
#    0 14 * * * cd /root/AspanLab/trustcontrol/marketing && python threads_bot.py --once
# ════════════════════════════════════════════════════════════

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# .env из marketing/ или trustcontrol/ (если установлен python-dotenv)
try:
    from dotenv import load_dotenv
    for _p in (Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"):
        if _p.exists():
            load_dotenv(_p)
            break
except Exception:
    pass

HERE         = Path(__file__).parent
STATE_FILE   = HERE / "threads_state.json"     # cooldown углов + последние посты
LOG_FILE     = HERE / "threads_posted.log"     # человекочитаемый журнал

OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
THREADS_UID  = os.getenv("THREADS_USER_ID", "")
THREADS_TOK  = os.getenv("THREADS_ACCESS_TOKEN", "")
TG_TOKEN     = os.getenv("THREADS_TG_TOKEN", "")
TG_CHAT      = os.getenv("THREADS_TG_CHAT", "")
MODE         = os.getenv("THREADS_MODE", "veto").lower()
HOLD_MIN     = int(os.getenv("THREADS_HOLD_MIN", "60"))

THREADS_MAX  = 500   # лимит символов поста в Threads

# ── Банк углов ──────────────────────────────────────────────────────────────
# kind: bait — наживка для комментов (охваты); product — позиционирование;
#       article — крючок со ссылкой на статью. brief — ЧТО сказать, GPT облекает
#       это в живой пост в стиле Данила. Бот берёт угол с самым старым last_used.
ANGLES = [
    # ── Наживки (engagement-first) ──────────────────────────────────────────
    {"id": "bait_colleagues", "kind": "bait",
     "brief": "Вопрос к тем кто работал на кассе: какую схему «левака» вы видели "
              "у КОЛЛЕГ (не у себя). Попроси писать в комменты, обещай собрать "
              "самые наглые в отдельный пост."},
    {"id": "bait_percent", "kind": "bait",
     "brief": "Спроси сколько процентов выручки малый бизнес в Казахстане теряет "
              "на леваке кассиров: 1%, 5% или 15%? Попроси написать ОДНУ цифру в "
              "ответ, пообещай потом назвать реальную."},
    {"id": "bait_phrases", "kind": "bait",
     "brief": "Классика на кассе: «терминал не работает, только наличкой» (а он "
              "работает). Спроси какие ещё фразы — признак что тебя разводят. "
              "Пусть дописывают свои в комменты."},
    {"id": "bait_debate", "kind": "bait",
     "brief": "Непопулярное мнение для спора: кассиры левачат не потому что воры, "
              "а потому что им мало платят и они уверены что не спалятся. Спроси "
              "согласны ли. Цель — поляризация владельцев и бывших кассиров."},
    {"id": "bait_riddle", "kind": "bait",
     "brief": "Загадка: у знакомого кассир за полгода увёл сумму на подержанную "
              "машину, а касса всё это время сходилась. Спроси как он это делал — "
              "пусть гадают схему в комментах."},
    {"id": "bait_owner_hit", "kind": "bait",
     "brief": "Прямой удар по владельцу: ты реально знаешь что говорят твоим "
              "клиентам на кассе когда тебя нет на точке? Или просто надеешься "
              "что всё ок?"},
    # ── Продуктовые / позиционирование ──────────────────────────────────────
    {"id": "prod_too_late", "kind": "product",
     "brief": "О краже узнаёшь поздно — не в момент когда кассир кладёт сдачу в "
              "карман, а когда кассовый разрыв уже большой и ты не знаешь кто "
              "именно, в какую смену и на сколько. Это не проблема учёта — никто "
              "не слушал."},
    {"id": "prod_not_pos", "kind": "product",
     "brief": "iiko, Poster, 1С считают что продали и сколько на складе, но не "
              "слышат как кассир говорит «давайте без чека, дешевле». Это не их "
              "задача. TrustControl — не касса, это микрофон который работает "
              "когда тебя нет рядом."},
    {"id": "prod_not_competitor", "kind": "product",
     "brief": "«Вы конкурируете с iiko?» — нет. iiko считает деньги, TrustControl "
              "слушает разговоры. Одно не заменяет другое. Если уже стоит iiko — "
              "поставь TrustControl рядом, микрофон ни с чем не конфликтует."},
    {"id": "prod_install", "kind": "product",
     "brief": "«Я уже один раз менял систему, больше не хочу». Понимаю. "
              "TrustControl ничего не заменяет: USB-микрофон + программа на "
              "кассовом ПК. Не трогает кассу, не лезет в базу. Установка 10 минут."},
    {"id": "prod_category", "kind": "product",
     "brief": "В Казахстане есть системы учёта, CRM и камеры. Но не было "
              "инструмента который слушает что именно говорят на кассе — не видео, "
              "не транзакции, а живой разговор. Это новая категория, мы её "
              "открываем в РК."},
    # ── Крючки со ссылкой на статью ─────────────────────────────────────────
    {"id": "art_leak", "kind": "article",
     "brief": "Касса сходится, а денег меньше. 4 схемы которые касса не видит: "
              "продажа без чека, возврат-призрак, недовес, свои цены для своих. "
              "Намекни что подробный разбор по ссылке. (Ссылку добавит бот.)"},
]

STYLE = (
    "Ты — основатель TrustControl (ИИ-мониторинг качества обслуживания и "
    "честности на кассе для малого бизнеса Казахстана). Пишешь пост в Threads "
    "от первого лица. Голос: прямой, простой, без корпоратива и без эмодзи-"
    "спама (1-2 эмодзи максимум, можно ноль). Короткие строки, пустые строки "
    "между мыслями (в Threads читают вертикально). Разговорный, как будто "
    "пишешь знакомому предпринимателю. Без хэштегов в теле (можно 1 в конце). "
    f"ЖЁСТКИЙ лимит {THREADS_MAX} символов — лучше короче. Не используй "
    "маркетинговые клише («революционный», «инновационный»). Цель поста ниже."
)


# ── Состояние (cooldown + анти-повтор) ──────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"last_used": {}, "recent_posts": []}


def _save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")


def _pick_angle(st: dict) -> dict:
    """Берёт угол с самым старым last_used (никогда не использованный = 0)."""
    last = st.get("last_used", {})
    # сортируем: сначала те, что дольше всего не выходили; среди равных — случайно
    ordered = sorted(ANGLES, key=lambda a: (last.get(a["id"], 0), random.random()))
    return ordered[0]


# ── Генерация поста ─────────────────────────────────────────────────────────

def generate_post(angle: dict, st: dict) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_KEY)

    recent = "\n---\n".join(st.get("recent_posts", [])[-6:])
    avoid = (
        f"\n\nНЕ повторяй формулировки и структуру этих недавних постов:\n{recent}"
        if recent else ""
    )
    link = ""
    if angle["kind"] == "article":
        link = "\n\nВ конце добавь строкой ссылку: trustcontrol.kz"

    prompt = f"{STYLE}\n\nЦель этого поста:\n{angle['brief']}{link}{avoid}\n\nВыдай ТОЛЬКО текст поста, без кавычек и пояснений."

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.9,
        max_tokens=400,
    )
    text = (resp.choices[0].message.content or "").strip().strip('"')
    if len(text) > THREADS_MAX:
        text = text[:THREADS_MAX].rsplit("\n", 1)[0].rstrip()
    return text


# ── Публикация в Threads (Graph API, 2 шага) ────────────────────────────────

def publish_threads(text: str) -> str | None:
    """Создаёт контейнер и публикует. Возвращает id поста или None."""
    if not (THREADS_UID and THREADS_TOK):
        print("⚠️ THREADS_USER_ID / THREADS_ACCESS_TOKEN не заданы — пропускаю публикацию")
        return None
    base = f"https://graph.threads.net/v1.0/{THREADS_UID}"
    try:
        r1 = requests.post(base + "/threads", data={
            "media_type": "TEXT", "text": text, "access_token": THREADS_TOK,
        }, timeout=30)
        r1.raise_for_status()
        creation_id = r1.json().get("id")
        if not creation_id:
            print(f"⚠️ Threads: нет creation_id: {r1.text}")
            return None
        # Meta рекомендует ~30с паузы между созданием и публикацией
        time.sleep(5)
        r2 = requests.post(base + "/threads_publish", data={
            "creation_id": creation_id, "access_token": THREADS_TOK,
        }, timeout=30)
        r2.raise_for_status()
        post_id = r2.json().get("id")
        print(f"✅ Опубликовано в Threads: {post_id}")
        return post_id
    except Exception as e:
        print(f"❌ Ошибка публикации в Threads: {e}")
        return None


# ── Telegram (контентный бот) для вето ──────────────────────────────────────

def _tg(method: str, **params):
    if not TG_TOKEN:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
            json=params, timeout=20,
        )
        return r.json()
    except Exception as e:
        print(f"⚠️ Telegram {method}: {e}")
        return None


def send_draft(text: str, draft_id: str):
    kb = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"thr_pub:{draft_id}"},
        {"text": "🔁 Другой",       "callback_data": f"thr_regen:{draft_id}"},
        {"text": "❌ Пропустить",    "callback_data": f"thr_skip:{draft_id}"},
    ]]}
    msg = (
        f"📝 Черновик для Threads (опубликую через {HOLD_MIN} мин, если не тронешь):\n\n"
        f"{text}"
    )
    _tg("sendMessage", chat_id=TG_CHAT, text=msg, reply_markup=kb)


def wait_for_veto(draft_id: str) -> str:
    """Ждёт нажатие кнопки до HOLD_MIN. Возвращает: pub | skip | regen | timeout."""
    deadline = time.time() + HOLD_MIN * 60
    offset = None
    # сбрасываем накопленные апдейты, чтобы старые кнопки не сработали
    seed = _tg("getUpdates", timeout=0)
    if seed and seed.get("result"):
        offset = seed["result"][-1]["update_id"] + 1
    while time.time() < deadline:
        upd = _tg("getUpdates", offset=offset, timeout=20)
        if upd and upd.get("result"):
            for u in upd["result"]:
                offset = u["update_id"] + 1
                cb = u.get("callback_query")
                if not cb:
                    continue
                data = cb.get("data", "")
                _tg("answerCallbackQuery", callback_query_id=cb.get("id", ""))
                if data == f"thr_pub:{draft_id}":
                    return "pub"
                if data == f"thr_skip:{draft_id}":
                    return "skip"
                if data == f"thr_regen:{draft_id}":
                    return "regen"
        time.sleep(2)
    return "timeout"


# ── Один цикл ───────────────────────────────────────────────────────────────

def run_once(dry: bool = False):
    st = _load_state()
    angle = _pick_angle(st)
    print(f"→ Угол: {angle['id']} ({angle['kind']})")

    text = generate_post(angle, st)
    print("\n──── ЧЕРНОВИК ────\n" + text + "\n──────────────────\n")

    if dry:
        print("(dry-run: ничего не публикую)")
        return

    decision = "pub"
    if MODE == "draft":
        send_draft(text, str(int(time.time())))
        print("Режим draft: отправил в Telegram, не публикую.")
        return
    if MODE == "veto" and TG_TOKEN and TG_CHAT:
        draft_id = str(int(time.time()))
        send_draft(text, draft_id)
        for _ in range(3):  # до 3 регенераций
            decision = wait_for_veto(draft_id)
            if decision != "regen":
                break
            text = generate_post(angle, st)
            draft_id = str(int(time.time()))
            send_draft(text, draft_id)
        if decision == "skip":
            print("⏭ Пропущено по твоей команде.")
            return
        # pub или timeout → публикуем

    post_id = publish_threads(text)

    # фиксируем в состоянии (даже если публикация не прошла — чтобы не зациклить угол)
    st.setdefault("last_used", {})[angle["id"]] = int(time.time())
    st.setdefault("recent_posts", []).append(text)
    st["recent_posts"] = st["recent_posts"][-12:]
    _save_state(st)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        f.write(f"\n=== {ts} · {angle['id']} · post={post_id} ===\n{text}\n")


# ── Демон: сам ждёт нужного часа ────────────────────────────────────────────

def run_loop():
    post_hour_utc = int(os.getenv("THREADS_POST_HOUR_UTC", "15"))  # 20:00 Алматы
    print(f"Демон запущен. Пощу раз в день в {post_hour_utc}:00 UTC.")
    posted_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if now.hour == post_hour_utc and posted_date != today:
            try:
                run_once()
            except Exception as e:
                print(f"❌ Цикл упал: {e}")
            posted_date = today
        time.sleep(300)  # проверка раз в 5 минут


def main():
    ap = argparse.ArgumentParser(description="TrustControl Threads автопостинг")
    ap.add_argument("--dry",  action="store_true", help="сгенерить и показать, не публиковать")
    ap.add_argument("--once", action="store_true", help="один цикл (для cron)")
    ap.add_argument("--loop", action="store_true", help="демон с расписанием")
    args = ap.parse_args()

    if not OPENAI_KEY:
        raise SystemExit("OPENAI_API_KEY не задан")
    if args.loop:
        run_loop()
    else:
        run_once(dry=args.dry)


if __name__ == "__main__":
    main()
