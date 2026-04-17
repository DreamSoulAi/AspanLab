# ════════════════════════════════════════════════════════════
#  API: POS-транзакции (данные кассы)
#
#  Эндпоинты:
#    POST /api/v1/pos/transaction  — принять чек от кассового ПО
#    GET  /api/v1/pos/gaps         — список подозрительных разрывов
#
#  Авторизация: JWT (владелец бизнеса) для чтения,
#               API-ключ точки для записи (от кассового ПО).
# ════════════════════════════════════════════════════════════

import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db, AsyncSessionLocal
from backend.models.pos_transaction import PosTransaction
from backend.models.report import Report
from backend.models.location import Location
from backend.models.alert import Alert
from backend.models.user import User
from backend.api.auth import get_current_user
from backend.services.pos_matcher import match_report_with_pos
from backend.services import notifier

log = logging.getLogger("pos")
router = APIRouter()


class TransactionIn(BaseModel):
    timestamp:  datetime
    amount:     float                    # сумма в тенге
    receipt_id: Optional[str] = None     # номер чека
    cashier_id: Optional[str] = None     # ID кассира
    currency:   str = "KZT"
    raw_data:   Optional[str] = None     # любой JSON от кассы (строка)


async def _get_location_by_key(api_key: str, db: AsyncSession) -> Location:
    result = await db.execute(
        select(Location).where(
            Location.api_key  == api_key,
            Location.is_active == True,
        )
    )
    loc = result.scalar()
    if not loc:
        raise HTTPException(status_code=401, detail="Неверный API ключ точки")
    return loc


# ── POST /transaction ─────────────────────────────────────────────────────────

@router.post("/transaction")
async def receive_transaction(
    background_tasks: BackgroundTasks,
    tx: TransactionIn,
    api_key:   Optional[str] = Header(None, alias="X-API-Key"),
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Принимает данные чека от кассового ПО.
    Сразу запускает фоновую сверку с аудио-отчётами.
    """
    effective_key = (api_key or x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(status_code=401, detail="Нужен X-API-Key")

    location = await _get_location_by_key(effective_key, db)

    pos_tx = PosTransaction(
        location_id=location.id,
        timestamp=tx.timestamp,
        amount=tx.amount,
        receipt_id=tx.receipt_id,
        cashier_id=tx.cashier_id,
        currency=tx.currency,
        raw_data=tx.raw_data,
    )
    db.add(pos_tx)
    await db.commit()
    await db.refresh(pos_tx)

    background_tasks.add_task(
        _match_transaction,
        pos_tx_id=pos_tx.id,
        location_id=location.id,
        location_name=location.name,
        telegram_chat=location.telegram_chat,
    )

    return {"status": "ok", "transaction_id": pos_tx.id}


async def _match_transaction(
    pos_tx_id: int,
    location_id: int,
    location_name: str,
    telegram_chat: Optional[str],
):
    """
    Фоновая задача: сопоставляет новый чек с аудио-отчётами.
    Если в ±2 мин есть payment_confirmed=true без соответствия → CRITICAL_FRAUD_RISK.
    """
    from datetime import timedelta

    async with AsyncSessionLocal() as db:
        tx_result = await db.execute(
            select(PosTransaction).where(PosTransaction.id == pos_tx_id)
        )
        pos_tx = tx_result.scalar()
        if not pos_tx:
            return

        window_start = pos_tx.timestamp - timedelta(minutes=2)
        window_end   = pos_tx.timestamp + timedelta(minutes=2)

        # Ищем отчёты с payment_confirmed=True в окне
        rep_result = await db.execute(
            select(Report).where(
                Report.location_id       == location_id,
                Report.payment_confirmed == True,
                Report.fraud_status      == "normal",
                Report.timestamp         >= window_start,
                Report.timestamp         <= window_end,
            )
        )
        reports = rep_result.scalars().all()

        for report in reports:
            new_status = await match_report_with_pos(report, db)
            if new_status == "critical_fraud_risk":
                report.fraud_status = new_status
                report.is_priority  = True

                # Тревога
                db.add(Alert(
                    location_id=location_id,
                    report_id=report.id,
                    alert_type="fraud",
                    severity="high",
                    transcript=report.transcript[:500] if report.transcript else "",
                    trigger_phrase="POS-разрыв: оплата без чека",
                ))

                if telegram_chat:
                    await notifier.send_critical_alert({
                        "telegram_chat": telegram_chat,
                        "location_name": location_name,
                        "summary": (
                            f"POS-разрыв: голос подтверждает оплату, "
                            f"но чека на сумму нет в кассе (±2 мин)"
                        ),
                        "audio_url": report.s3_url,
                        "sha256":    report.audio_sha256,
                    })

        await db.commit()


# ── POST /webhook ────────────────────────────────────────────────────────────

def _detect_pos_type(raw: dict) -> str:
    """
    Определяет тип POS-системы по структуре входящего JSON.
    Поддерживает: Rosta, 1C, iiko, r_keeper и любой другой формат (none).
    """
    raw_str = json.dumps(raw).lower()
    if "rostaid" in raw_str or "rosta" in raw:
        return "rosta"
    if any(k in raw for k in ("guid", "1c", "УТ")):
        return "1c"
    if "iiko" in raw_str or "iikoId" in raw:
        return "iiko"
    if "rkeeper" in raw_str or "r_keeper" in raw_str:
        return "keeper"
    return "none"


def _extract_universal(raw: dict) -> dict:
    """
    Универсальный парсер: находит стандартные поля вне зависимости от POS-системы.
    Поддерживает camelCase, snake_case, русские ключи, разные вложенности.
    """
    def _find(*keys):
        for k in keys:
            if k in raw and raw[k] is not None:
                return raw[k]
        return None

    # Сумма
    amount = _find("amount", "total", "totalAmount", "total_amount",
                   "sum", "итого", "сумма", "TotalSum", "summa")
    try:
        amount = float(str(amount).replace(",", ".").replace(" ", ""))
    except Exception:
        amount = 0.0

    # Позиции чека
    items_raw = _find("items", "products", "positions", "goods",
                      "rows", "lines", "товары", "позиции")
    items = []
    if isinstance(items_raw, list):
        for it in items_raw:
            if isinstance(it, dict):
                items.append({
                    "name":  it.get("name") or it.get("title") or it.get("наименование") or "?",
                    "qty":   it.get("qty") or it.get("quantity") or it.get("count") or 1,
                    "price": it.get("price") or it.get("sum") or it.get("amount") or 0,
                })

    # Время
    ts_raw = _find("date", "timestamp", "created_at", "dateTime",
                   "time", "check_date", "дата", "время")
    timestamp = datetime.utcnow()
    if ts_raw:
        try:
            if isinstance(ts_raw, (int, float)):
                timestamp = datetime.utcfromtimestamp(ts_raw)
            else:
                timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            pass

    # Номер чека
    receipt_id = _find("receipt_id", "id", "check_id", "number",
                       "receiptNumber", "номер_чека", "ReceiptId")
    if receipt_id is not None:
        receipt_id = str(receipt_id)

    return {
        "amount":     amount,
        "items":      items,
        "timestamp":  timestamp,
        "receipt_id": receipt_id,
    }


@router.post("/webhook")
async def pos_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key:   Optional[str] = Header(None, alias="X-API-Key"),
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Универсальный POS-webhook.
    Принимает JSON от любой кассовой системы (Rosta, 1C, iiko, r_keeper, кастом).
    Авторизация: X-API-Key точки.

    Автоматически определяет поля: amount, items, timestamp, receipt_id.
    """
    effective_key = (api_key or x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(status_code=401, detail="Нужен X-API-Key")

    location = await _get_location_by_key(effective_key, db)

    try:
        raw: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    parsed   = _extract_universal(raw)
    pos_type = _detect_pos_type(raw)

    pos_tx = PosTransaction(
        location_id=location.id,
        timestamp=parsed["timestamp"],
        amount=parsed["amount"],
        receipt_id=parsed["receipt_id"],
        currency="KZT",
        pos_type=pos_type,
        items=parsed["items"],
        raw_data=json.dumps(raw, ensure_ascii=False)[:4000],
    )
    db.add(pos_tx)
    await db.commit()
    await db.refresh(pos_tx)

    log.info(
        f"[loc={location.id}] Webhook {pos_type}: "
        f"amount={parsed['amount']} ₸, items={len(parsed['items'])}, "
        f"receipt={parsed['receipt_id']}"
    )

    background_tasks.add_task(
        _match_transaction,
        pos_tx_id=pos_tx.id,
        location_id=location.id,
        location_name=location.name,
        telegram_chat=location.telegram_chat,
    )

    return {
        "status":        "ok",
        "transaction_id": pos_tx.id,
        "pos_type":      pos_type,
        "amount":        parsed["amount"],
        "items_count":   len(parsed["items"]),
    }


# ── GET /gaps ─────────────────────────────────────────────────────────────────

@router.get("/gaps")
async def get_fraud_gaps(
    location_id: Optional[int] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Список отчётов со статусом CRITICAL_FRAUD_RISK."""
    limit = min(limit, 200)

    locs = await db.execute(
        select(Location.id).where(Location.owner_id == user.id)
    )
    user_locs = [r[0] for r in locs.all()]
    if not user_locs:
        return []

    query = (
        select(Report)
        .where(
            Report.location_id.in_(user_locs),
            Report.fraud_status == "critical_fraud_risk",
        )
        .order_by(Report.timestamp.desc())
        .limit(limit)
    )
    if location_id:
        if location_id not in user_locs:
            raise HTTPException(status_code=403, detail="Нет доступа к этой точке")
        query = query.where(Report.location_id == location_id)

    result = await db.execute(query)
    rows = result.scalars().all()

    return [
        {
            "id":           r.id,
            "timestamp":    r.timestamp.isoformat(),
            "transcript":   (r.transcript or "")[:300],
            "fraud_status": r.fraud_status,
            "gpt_summary":  r.gpt_summary,
            "s3_url":       r.s3_url,
            "audio_sha256": r.audio_sha256,
        }
        for r in rows
    ]
