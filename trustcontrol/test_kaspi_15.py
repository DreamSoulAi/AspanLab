#!/usr/bin/env python3
# Шаг 3 — прогон 15 диалогов через РЕАЛЬНУЮ check_kaspi_fraud().
# Проверяем: ложняки на QR-оплате, мягкий путь при пустом списке, ловлю фрода.
import sys
from backend.services.kaspi_detector import check_kaspi_fraud, has_transfer_intent

# (диалог, allowed_phones, payment_mode, ОЖИДАЕМ: "norm"/"high"/"low")
CASES = [
    # 1. Обычная QR-оплата со словом "каспи", БЕЗ intent → норма
    ("Оплатите на Каспи по QR, пожалуйста. Спасибо.", [], "mixed", "norm"),
    # 2. QR + продиктован номер, но БЕЗ просьбы перевести → норма
    ("Оплата на Каспи, вот QR. Если что мой рабочий 8 707 111 22 33.", [], "mixed", "norm"),
    # 3. Клиент сам называет свой номер без intent → норма
    ("Запишите меня, мой номер 8 707 555 44 33, перезвоните завтра.", [], "mixed", "norm"),
    # 4. mixed, intent + чужой номер, список пуст → low (мягкий)
    ("Аппарат не работает, переведите на этот номер 8 707 123 45 67.", [], "mixed", "low"),
    # 5. mixed, intent + номер НЕ в списке → high
    ("Терминал завис, перекинь на 8 707 999 88 77.", ["+77071112233"], "mixed", "high"),
    # 6. mixed, intent + номер В списке → норма
    ("Переведите на наш номер 8 707 111 22 33.", ["87071112233"], "mixed", "norm"),
    # 7. transfers_ok, легальный номер из allowed_phones → норма
    ("Переведите на 8 707 111 22 33, это касса.", ["8 707 111 22 33"], "transfers_ok", "norm"),
    # 8. transfers_ok, чужой номер → high
    ("Переведи мне лично на 8 707 555 00 00.", ["+77071112233"], "transfers_ok", "high"),
    # 9. qr_only, "переведи на номер", список ПУСТ → high (переводы недопустимы)
    ("Касса не пробивает, переведи на номер 8 707 123 45 67.", [], "qr_only", "high"),
    # 10. qr_only, intent + номер, даже "свой" в списке → high
    ("Переведи на 8 707 111 22 33, так быстрее.", ["+77071112233"], "qr_only", "high"),
    # 11. cash_only, intent + номер → high
    ("Сдачи нет, скинь на этот номер 8 707 888 99 00.", [], "cash_only", "high"),
    # 12. Казахский intent "аудар нөмірге" + чужой номер → high
    ("Аппарат істемейді, осы нөмірге аударыңыз 8 707 777 88 99.", ["+77071112233"], "mixed", "high"),
    # 13. Казахский intent, список пуст → low
    ("Терминал жоқ, мына нөмірге аудар 8 707 666 55 44.", [], "mixed", "low"),
    # 14. qr_only, обычная QR-оплата "каспи" БЕЗ intent → норма (не ловить!)
    ("На Каспи по QR оплатите. Готово, спасибо.", [], "qr_only", "norm"),
    # 15. cash_only, болтовня про каспи без intent и без номера → норма
    ("У меня Каспи Голд есть, кэшбек хороший. Наличными возьмёте?", [], "cash_only", "norm"),
]


def classify(hits):
    if not hits:
        return "norm"
    return "high" if any(h.get("confidence") == "high" for h in hits) else "low"


def verdict(expected, got):
    if expected == got:
        return "✅ верно"
    # ложняк = ждали norm, получили high/low ; пропуск = ждали high/low, получили norm
    if expected == "norm" and got != "norm":
        return "❌ ЛОЖНЯК"
    if expected != "norm" and got == "norm":
        return "❌ ПРОПУСК"
    return f"⚠️ не тот уровень (ждали {expected}, got {got})"


def main():
    correct = ложняк = пропуск = wrong_level = 0
    print("=" * 100)
    for i, (text, allowed, mode, expected) in enumerate(CASES, 1):
        hits = check_kaspi_fraud(text, allowed, mode)
        got = classify(hits)
        intent = "да" if has_transfer_intent(text) else "нет"
        v = verdict(expected, got)
        if v.startswith("✅"):
            correct += 1
        elif "ЛОЖНЯК" in v:
            ложняк += 1
        elif "ПРОПУСК" in v:
            пропуск += 1
        else:
            wrong_level += 1
        print(f"\n#{i:>2} | intent={intent:>3} | режим={mode:<12} | ждали={expected:<4} got={got:<4} | {v}")
        print(f"     диалог: {text}")
        if hits:
            print(f"     номера: {[(h['phone'], h['confidence']) for h in hits]}")
    print("\n" + "=" * 100)
    print(f"СВОДКА: верных {correct}/15 | ложняков {ложняк} | пропусков {пропуск} | не-тот-уровень {wrong_level}")
    print("=" * 100)
    return 0 if (ложняк == 0 and пропуск == 0 and wrong_level == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
