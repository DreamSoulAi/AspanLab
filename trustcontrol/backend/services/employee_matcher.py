# ════════════════════════════════════════════════════════════
#  Сервис: Привязка разговора к сотруднику по времени смены
#
#  Владелец задаёт смены: [{"name": "Айгуль", "start": 10, "end": 22}, ...]
#  start/end — часы по местному времени (Казахстан, UTC+5).
#  Окно может переходить через полночь: start=22, end=10 → ночь.
#  По времени разговора определяем кто стоял за кассой.
# ════════════════════════════════════════════════════════════

from datetime import datetime

# Казахстан — единый часовой пояс UTC+5. Сервер хранит время в UTC,
# а владелец вводит часы смен по местному времени.
KZ_UTC_OFFSET = 5


def match_employee(employees: list | None, ts_utc: datetime) -> str | None:
    """
    Возвращает имя сотрудника, который был на кассе в момент ts_utc.

    Логика:
      • нет сотрудников           → None
      • один сотрудник            → все разговоры на него (даже без окна)
      • несколько                → по окну смены (местное время)
      • никто не подошёл по окну  → None
    """
    if not employees:
        return None

    valid = [e for e in employees if isinstance(e, dict) and e.get("name")]
    if not valid:
        return None

    # Один сотрудник — всё пишем на него (касса с одним кассиром).
    if len(valid) == 1:
        return valid[0]["name"]

    local_hour = (ts_utc.hour + KZ_UTC_OFFSET) % 24

    for emp in valid:
        start = emp.get("start")
        end   = emp.get("end")
        # Без корректного окна — пропускаем (не сможем сопоставить по времени)
        if start is None or end is None:
            continue
        try:
            start = int(start) % 24
            end   = int(end) % 24
        except (TypeError, ValueError):
            continue

        if start == end:
            # Окно на все 24 часа
            return emp["name"]
        if start < end:
            if start <= local_hour < end:
                return emp["name"]
        else:
            # Окно переходит через полночь (например 22 → 10)
            if local_hour >= start or local_hour < end:
                return emp["name"]

    return None
