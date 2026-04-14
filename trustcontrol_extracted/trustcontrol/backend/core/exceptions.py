# ════════════════════════════════════════════════════════════
#  Кастомные исключения — чистая обработка ошибок
# ════════════════════════════════════════════════════════════

from fastapi import HTTPException


class NotFound(HTTPException):
    def __init__(self, detail: str = "Не найдено"):
        super().__init__(status_code=404, detail=detail)


class Forbidden(HTTPException):
    def __init__(self, detail: str = "Нет доступа"):
        super().__init__(status_code=403, detail=detail)


class BadRequest(HTTPException):
    def __init__(self, detail: str = "Неверный запрос"):
        super().__init__(status_code=400, detail=detail)


class TooLarge(HTTPException):
    def __init__(self, detail: str = "Файл слишком большой"):
        super().__init__(status_code=413, detail=detail)


class PlanLimitReached(HTTPException):
    def __init__(self, plan: str, limit: int):
        super().__init__(
            status_code=403,
            detail=f"Тариф «{plan}» позволяет максимум {limit} точек. Обновите тариф."
        )


class SubscriptionExpired(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=402,
            detail="Подписка истекла. Продлите тариф в личном кабинете."
        )
