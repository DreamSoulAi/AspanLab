# ════════════════════════════════════════════════════════════
#  TrustControl — Настройки
#  SECURITY: все секреты только из .env, никаких дефолтов
# ════════════════════════════════════════════════════════════

import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    # ── Сервер ───────────────────────────────────────────────
    PORT:  int  = int(os.getenv("PORT", 8000))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # ── База данных ──────────────────────────────────────────
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{BASE_DIR}/trustcontrol.db"
    )

    # ── API ключи — ОБЯЗАТЕЛЬНЫ в .env ──────────────────────
    OPENAI_API_KEY:    str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    TELEGRAM_BOT_TOKEN:str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # ── SECURITY: SECRET_KEY ─────────────────────────────────
    _secret = os.getenv("SECRET_KEY", "")
    if not _secret or len(_secret) < 32:
        # Generate a temporary key — app starts, but tokens reset on each restart.
        # Operators MUST set SECRET_KEY in Render env vars for persistence.
        _secret = secrets.token_hex(32)
        print("⚠️  WARNING: SECRET_KEY не задан — используется временный ключ.")
        print("⚠️  Установите SECRET_KEY в переменных окружения Render!")
    SECRET_KEY: str = _secret

    TOKEN_EXPIRE_HOURS: int = int(os.getenv("TOKEN_EXPIRE_HOURS", 24))

    # ── Аудио ────────────────────────────────────────────────
    SAMPLE_RATE:     int   = 16000
    CHANNELS:        int   = 1
    FRAME_DURATION:  int   = 30
    SILENCE_SECONDS: float = 2.5
    MAX_SEGMENT_MIN: int   = 2

    # ── Whisper ──────────────────────────────────────────────
    WHISPER_MODEL:    str = "whisper-1"
    WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "ru")

    # ── S3 / Supabase Storage (архив приоритетных записей) ───
    S3_BUCKET:             str = os.getenv("S3_BUCKET", "")
    S3_REGION:             str = os.getenv("S3_REGION", "us-east-1")
    S3_ENDPOINT_URL:       str = os.getenv("S3_ENDPOINT_URL", "")   # пусто = AWS, иначе Supabase/MinIO
    AWS_ACCESS_KEY_ID:     str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")

    # ── Kaspi ────────────────────────────────────────────────
    KASPI_NUMBER: str = os.getenv("KASPI_NUMBER", "")
    KASPI_NAME:   str = os.getenv("KASPI_NAME", "")

    # ── OTP режим ────────────────────────────────────────────
    # OTP_BYPASS=true → код всегда 000000, письмо не шлётся (только для теста!)
    OTP_BYPASS: bool = os.getenv("OTP_BYPASS", "false").lower() == "true"

    # ── Email (OTP-письма) ───────────────────────────────────
    # Вариант 1 (рекомендуется): Resend HTTP API — работает на Render
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    # Вариант 2: любой SMTP (Gmail, Yandex, Brevo…)
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASS: str = os.getenv("SMTP_PASS", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")

    # ── OTP / Auth ───────────────────────────────────────────
    # OTP_BYPASS=true → код всегда 000000 (только для dev/тестов, НИКОГДА в проде)
    OTP_BYPASS: bool = os.getenv("OTP_BYPASS", "false").lower() == "true"

    # ── Telegram бот ─────────────────────────────────────────
    # Имя бота без @ — показывается кнопкой "Получить код в Telegram"
    TELEGRAM_BOT_USERNAME: str = os.getenv("TELEGRAM_BOT_USERNAME", "")

    def validate(self):
        """Log warnings for missing optional env vars — never crash on startup."""
        if not self.OPENAI_API_KEY:
            print("⚠️  WARNING: OPENAI_API_KEY не задан — транскрипция не будет работать")
        if not self.TELEGRAM_BOT_TOKEN:
            print("⚠️  WARNING: TELEGRAM_BOT_TOKEN не задан — уведомления не будут работать")


settings = Settings()
settings.validate()
