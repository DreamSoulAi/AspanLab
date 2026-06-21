# ════════════════════════════════════════════════════════════
#  Сервис: S3 Retention Policy
#
#  Запускается как фоновая задача каждые 4 часа.
#
#  Правила хранения:
#  • Обычные записи (fraud_status=normal)   → удалить из S3 через 48 ч
#  • CRITICAL_FRAUD_RISK / is_priority=True → переместить в evidence/,
#    хранить 30 дней, SHA-256 гарантирует целостность
#  • Удалённые файлы помечаются s3_deleted_at в БД
# ════════════════════════════════════════════════════════════

import logging
import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.models.report import Report
from backend.database import AsyncSessionLocal

log = logging.getLogger("retention")

# Сроки хранения настраиваются через env (для временного накопления реальных
# записей под тест STT можно поднять RETENTION_NORMAL_TTL_HOURS, напр. 336 = 14 дней,
# потом вернуть 48). Дефолты — прежнее поведение.
NORMAL_TTL_HOURS   = int(os.getenv("RETENTION_NORMAL_TTL_HOURS", "48"))
EVIDENCE_TTL_DAYS  = int(os.getenv("RETENTION_EVIDENCE_TTL_DAYS", "30"))
EVIDENCE_PREFIX    = "evidence"


def _get_s3():
    """Создаёт S3-клиент из настроек."""
    from backend.config import settings
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION or "us-east-1",
    ), settings.S3_BUCKET


def _is_evidence(report: Report) -> bool:
    return report.fraud_status == "critical_fraud_risk" or report.is_priority


async def run_retention() -> dict:
    """
    Основная функция очистки. Вызывается из main.py каждые 4 часа.
    Возвращает статистику: {deleted, archived, skipped}.
    """
    from backend.config import settings
    if not settings.S3_BUCKET:
        log.debug("S3_BUCKET не задан — retention пропущен")
        return {"deleted": 0, "archived": 0, "skipped": 0}

    stats = {"deleted": 0, "archived": 0, "skipped": 0}
    now = datetime.utcnow()

    try:
        s3, bucket = _get_s3()
    except Exception as e:
        log.error(f"S3 клиент не создан: {e}")
        return stats

    async with AsyncSessionLocal() as db:
        # Ищем отчёты с S3-файлами которые ещё не удалены
        result = await db.execute(
            select(Report).where(
                Report.s3_key       != None,
                Report.s3_deleted_at == None,
            )
        )
        reports = result.scalars().all()

        for report in reports:
            key = report.s3_key
            if not key:
                stats["skipped"] += 1
                continue

            age = now - report.timestamp

            if _is_evidence(report):
                # Критические записи: переносим в evidence/ если ещё не там
                if not key.startswith(EVIDENCE_PREFIX + "/"):
                    new_key = f"{EVIDENCE_PREFIX}/{key}"
                    try:
                        s3.copy_object(
                            Bucket=bucket, CopySource={"Bucket": bucket, "Key": key},
                            Key=new_key,
                        )
                        s3.delete_object(Bucket=bucket, Key=key)

                        report.s3_key = new_key
                        log.info(f"[report={report.id}] Архивирован: {new_key}")
                        stats["archived"] += 1
                    except Exception as e:
                        log.error(f"[report={report.id}] Ошибка архивирования: {e}")
                        stats["skipped"] += 1
                        continue

                # Если пролежал 30 дней — удаляем и архив
                if age > timedelta(days=EVIDENCE_TTL_DAYS):
                    _delete_s3_file(s3, bucket, key, report, stats)

            else:
                # Обычные записи: удаляем через 48 ч
                if age > timedelta(hours=NORMAL_TTL_HOURS):
                    _delete_s3_file(s3, bucket, key, report, stats)
                else:
                    stats["skipped"] += 1

        await db.commit()

    log.info(
        f"Retention завершён: удалено={stats['deleted']} "
        f"архивировано={stats['archived']} пропущено={stats['skipped']}"
    )
    return stats


def _delete_s3_file(s3, bucket: str, key: str, report: Report, stats: dict):
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        report.s3_deleted_at = datetime.utcnow()
        log.info(f"[report={report.id}] S3 файл удалён: {key}")
        stats["deleted"] += 1
    except Exception as e:
        log.error(f"[report={report.id}] Ошибка удаления S3: {e}")
        stats["skipped"] += 1
