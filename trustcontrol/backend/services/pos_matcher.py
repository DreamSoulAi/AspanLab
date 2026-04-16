# ════════════════════════════════════════════════════════════
#  Сервис: Детектор «Кассового разрыва»
#
#  Логика:
#  1. Из транскрипта извлекаем суммы (regex + слова-числа)
#  2. Ищем POS-транзакцию в окне ±2 минуты от времени отчёта
#  3. Если payment_confirmed=true НО чека нет → CRITICAL_FRAUD_RISK
#  4. Если чек есть → is_matched=true на PosTransaction
# ════════════════════════════════════════════════════════════

import re
import logging
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from backend.models.pos_transaction import PosTransaction
from backend.models.report import Report

log = logging.getLogger("pos_matcher")

# Окно сопоставления: ищем чек в ±N минут от времени разговора
MATCH_WINDOW_MINUTES = 2

# Допустимое отклонение суммы (10%) — на случай скидок / округлений
AMOUNT_TOLERANCE = 0.10

# Числа-слова в русском (упрощённый набор для сумм)
_RU_WORDS = {
    "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900, "тысяча": 1000, "тысячи": 1000, "тысяч": 1000,
    "две тысячи": 2000, "три тысячи": 3000, "четыре тысячи": 4000,
    "пять тысяч": 5000, "десять тысяч": 10000,
}

# Казахские числа (основные)
_KK_WORDS = {
    "бір": 1, "екі": 2, "үш": 3, "төрт": 4, "бес": 5,
    "алты": 6, "жеті": 7, "сегіз": 8, "тоғыз": 9, "он": 10,
    "жүз": 100, "мың": 1000, "екі мың": 2000, "бес мың": 5000,
}


def extract_amounts(text: str) -> list[float]:
    """
    Извлекает денежные суммы из транскрипта.
    Возвращает список чисел (тенге).
    """
    amounts = set()
    t = text.lower()

    # Цифровые паттерны: 5000, 5 000, 5,000, 2500₸, 500 тг, итого 1200
    for m in re.finditer(
        r"(?:итого|с\s+вас|сумма|оплата|чек|стоит|цена|всего)[\s:]*"
        r"([\d\s,]+)",
        t
    ):
        raw = m.group(1).replace(" ", "").replace(",", "")
        if raw.isdigit():
            amounts.add(float(raw))

    # Любые числа рядом с ₸ / тенге / тг
    for m in re.finditer(r"(\d[\d\s,]*)\s*(?:₸|тенге|тг\b)", t):
        raw = m.group(1).replace(" ", "").replace(",", "")
        if raw.isdigit():
            amounts.add(float(raw))

    # Просто числа 3+ знаков (возможные суммы)
    for m in re.finditer(r"\b(\d{3,6})\b", t):
        amounts.add(float(m.group(1)))

    # Слова-числа (ru + kk)
    for word, val in {**_RU_WORDS, **_KK_WORDS}.items():
        if re.search(r"\b" + re.escape(word) + r"\b", t):
            amounts.add(float(val))

    return sorted(amounts)


def _amounts_match(extracted: list[float], pos_amount: float) -> bool:
    """Проверяет совпадение суммы с допуском AMOUNT_TOLERANCE."""
    tol = pos_amount * AMOUNT_TOLERANCE
    return any(abs(a - pos_amount) <= max(tol, 50) for a in extracted)


async def match_report_with_pos(
    report: Report,
    db: AsyncSession,
) -> str:
    """
    Сопоставляет один отчёт с POS-транзакциями.
    Возвращает новый fraud_status: "normal" | "critical_fraud_risk".

    Вызывается только когда report.payment_confirmed = True.
    """
    window_start = report.timestamp - timedelta(minutes=MATCH_WINDOW_MINUTES)
    window_end   = report.timestamp + timedelta(minutes=MATCH_WINDOW_MINUTES)

    result = await db.execute(
        select(PosTransaction).where(
            PosTransaction.location_id == report.location_id,
            PosTransaction.timestamp   >= window_start,
            PosTransaction.timestamp   <= window_end,
            PosTransaction.is_matched  == False,
        )
    )
    candidates = result.scalars().all()

    if not candidates:
        log.warning(
            f"[report={report.id}] payment_confirmed=True но POS-чека нет "
            f"в окне ±{MATCH_WINDOW_MINUTES} мин → CRITICAL_FRAUD_RISK"
        )
        return "critical_fraud_risk"

    # Пробуем найти совпадение по сумме
    extracted = extract_amounts(report.transcript or "")
    for tx in candidates:
        if not extracted or _amounts_match(extracted, tx.amount):
            await db.execute(
                update(PosTransaction)
                .where(PosTransaction.id == tx.id)
                .values(is_matched=True, matched_report_id=report.id)
            )
            log.info(
                f"[report={report.id}] Сопоставлен с POS-чеком #{tx.id} "
                f"на сумму {tx.amount} ₸"
            )
            return "normal"

    log.warning(
        f"[report={report.id}] Чек есть, но сумма не совпадает "
        f"(extracted={extracted}) → CRITICAL_FRAUD_RISK"
    )
    return "critical_fraud_risk"


async def run_pos_matching_for_location(
    location_id: int,
    db: AsyncSession,
    lookback_minutes: int = 30,
) -> int:
    """
    Пакетная проверка: проходит по всем несопоставленным
    payment_confirmed=true отчётам за последние N минут.
    Возвращает количество отчётов со статусом CRITICAL_FRAUD_RISK.
    """
    since = datetime.utcnow() - timedelta(minutes=lookback_minutes)

    result = await db.execute(
        select(Report).where(
            Report.location_id       == location_id,
            Report.payment_confirmed == True,
            Report.fraud_status      == "normal",
            Report.timestamp         >= since,
        )
    )
    reports = result.scalars().all()
    critical_count = 0

    for report in reports:
        new_status = await match_report_with_pos(report, db)
        if new_status != report.fraud_status:
            report.fraud_status = new_status
            if new_status == "critical_fraud_risk":
                critical_count += 1

    if reports:
        await db.commit()

    return critical_count
