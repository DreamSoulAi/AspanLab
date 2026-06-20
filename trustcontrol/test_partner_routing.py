"""
test_partner_routing.py — проверка логики детекции галлюцинаций и роутинга.

Тестирует ТОЛЬКО детерминированный слой (layer1) — без сетевых вызовов.
Запуск: python test_partner_routing.py

Кейсы:
  A. Нормальный казахский текст (тест1-KK из sravnenie.py) → NOT broken
  B. Loop-галлюцинация (тест2-KK: біріншін×40) → broken, причина loop_collapse
  C. Каша-галлюцинация (тест3-RU: салас×50) → broken, причина loop_collapse
  D. Нормальный русский текст (тест2-RU из sravnenie.py) → NOT broken
  E. Нормальный смешанный текст (тест3-KK) → NOT broken
  F. Короткий легитимный повтор ("да-да-да 590 тенге") → NOT broken
  G. Пустая строка → broken (empty)

  TTR-таблица: показывает числа для нормальной речи vs галлюцинации.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from backend.services.partner_stt import _hallucination_layer1, _ttr, _strip_repeat_loops

TESTS = [
    (
        "A",
        "Нормальный казахский (тест1-KK)",
        "алма ау болды болды айбыл сейлесеніз ассамалайкем не заказ берінісзе келеді сізалматы ба бір "
        "көрейінізді рой және айырғанын бере салыңыз жақсы сізден мың тоғыз жүз тенге оплата карты "
        "или наличиіне моличка хорошо сосаңыз он бес минутта дайын болады зал орданияда күте тұрсаңыз болады",
        False,
    ),
    (
        "B",
        "Loop-галлюцинация (тест2-KK: біріншін×40)",
        " ".join(["күрметтің"] + ["біріншін"] * 40 + ["жаңағыңызға"] * 15),
        True,
    ),
    (
        "C",
        "Каша-галлюцинация (тест3-RU: салас×50)",
        "саломат спа не заказ берешься бурт дойер жени балмасын аж " + " ".join(["салас"] * 50),
        True,
    ),
    (
        "D",
        "Нормальный русский (тест2-RU)",
        "здравствуйте да здравствуйте что хотели заказать я хочу один донер заказать "
        "не выйдет в кассе у нас вот только переводом сейчас хорошо это в другом месте "
        "закажу вы можете же через банкомат закинуть и нам переводом сделать мне лень просто",
        False,
    ),
    (
        "E",
        "Нормальный смешанный (тест3-KK)",
        "саламатыс па не заказ бересіз бір дойер және балмасына ыны жақсы су сусын айыран "
        "қажет етпіс ба жоқ қажет емес ыны жақсы сізде мын тоғыз жүз теңге оплата карты "
        "наличка картрид жақсы он бес мүлті дайын болып туйетіңіз ие",
        False,
    ),
    (
        "F",
        "Легитимный повтор (да-да-да, числа)",
        "да да да конечно 590 тенге оплата QR пожалуйста хорошо до свидания",
        False,
    ),
    (
        "G",
        "Пустая строка",
        "",
        True,
    ),
]

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

print("=" * 70)
print("  ТЕСТ ДЕТЕКТОРА ГАЛЛЮЦИНАЦИЙ — partner_stt._hallucination_layer1")
print("=" * 70)
print()

print(f"  {'Текст':<45} {'TTR':>6}  {'После схлопывания':>6}  engine")
print(f"  {'-'*45} {'-'*6}  {'-'*6}  {'-'*20}")

all_ok = True
for tag, label, text, expect_broken in TESTS:
    broken, reason = _hallucination_layer1(text)
    ok = (broken == expect_broken)
    if not ok:
        all_ok = False

    ttr_val = _ttr(text) if text else 0.0
    stripped = _strip_repeat_loops(text) if text else ""
    orig_n = len(text.split()) if text else 0
    kept_n = len(stripped.split()) if stripped else 0
    ratio = f"{kept_n/orig_n:.2f}" if orig_n else "—"

    status = PASS if ok else FAIL
    direction = "→ broken" if broken else "→ ok    "
    print(f"  [{tag}] {label[:42]:<42} TTR={ttr_val:.2f}  ratio={ratio}  {direction}  {reason[:35]}")
    print(f"       {status}  (ожидали broken={expect_broken}, получили broken={broken})")
    print()

print("=" * 70)

# TTR сравнительная таблица
print()
print("  TTR-сравнение (нормальная речь vs галлюцинация):")
print(f"  {'тест1-KK норм.':<30}  TTR={_ttr(TESTS[0][2]):.3f}   (порог >0.25)")
print(f"  {'тест2-RU норм.':<30}  TTR={_ttr(TESTS[3][2]):.3f}   (порог >0.25)")
print(f"  {'тест3-KK смесь':<30}  TTR={_ttr(TESTS[4][2]):.3f}   (порог >0.25)")
print(f"  {'тест2-KK loop галл.':<30}  TTR={_ttr(TESTS[1][2]):.3f}   (порог <0.25 → broken)")
print(f"  {'тест3-RU loop галл.':<30}  TTR={_ttr(TESTS[2][2]):.3f}   (порог <0.25 → broken)")
print()
print(f"  Запас: норм.речь TTR≈0.50–0.70, галл. TTR≈0.03–0.12. Порог 0.25 — в середине.")
print()

if all_ok:
    print(f"  \033[32m✓ ВСЕ {len(TESTS)}/{len(TESTS)} ТЕСТОВ ПРОШЛИ\033[0m")
else:
    failed = sum(1 for tag, label, text, expect in TESTS
                 if _hallucination_layer1(text)[0] != expect)
    print(f"  \033[31m✗ ПРОВАЛЕНО: {failed}/{len(TESTS)}\033[0m")

print()
sys.exit(0 if all_ok else 1)
