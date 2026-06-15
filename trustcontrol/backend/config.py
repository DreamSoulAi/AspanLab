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
    # Normalize DATABASE_URL: Render/Railway provide postgres:// or postgresql://
    # SQLAlchemy async requires postgresql+asyncpg://
    _db_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{BASE_DIR}/trustcontrol.db"
    )
    if _db_url.startswith("postgres://"):
        _db_url = "postgresql+asyncpg://" + _db_url[len("postgres://"):]
    elif _db_url.startswith("postgresql://") and "+asyncpg" not in _db_url:
        _db_url = "postgresql+asyncpg://" + _db_url[len("postgresql://"):]
    DATABASE_URL: str = _db_url

    # ── API ключи — ОБЯЗАТЕЛЬНЫ в .env ──────────────────────
    OPENAI_API_KEY:    str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    TELEGRAM_BOT_TOKEN:str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # ── SECURITY: SECRET_KEY ─────────────────────────────────
    # ОБЯЗАТЕЛЬНО задать в Render env vars. В DEBUG можно работать
    # с временным ключом (но JWT инвалидируется при каждом рестарте).
    _secret = os.getenv("SECRET_KEY", "")
    if not _secret or len(_secret) < 32:
        if os.getenv("DEBUG", "false").lower() == "true":
            _secret = secrets.token_hex(32)
            print("⚠️  DEBUG: SECRET_KEY не задан — временный ключ (JWT сбросятся при рестарте).", flush=True)
        else:
            raise RuntimeError(
                "SECRET_KEY не задан или короче 32 символов. "
                "Установите переменную окружения SECRET_KEY (не менее 32 символов)."
            )
    SECRET_KEY: str = _secret

    TOKEN_EXPIRE_HOURS: int = int(os.getenv("TOKEN_EXPIRE_HOURS", 24))

    # ── Админ ────────────────────────────────────────────────
    # Телефон владельца платформы — этот юзер автоматически
    # помечается is_admin=true при старте (не блокируется триалом).
    ADMIN_PHONE: str = os.getenv("ADMIN_PHONE", "").strip()

    # Отдельный Telegram-чат для ТЕХНИЧЕСКОЙ диагностики STT (сравнение движков,
    # «воркер не ответил», IGNORE/short_skip). Чтобы инженерный мусор НИКОГДА не
    # попадал в чат владельца. Пусто (по умолчанию) → диагностика только в логи,
    # клиент видит лишь чистые отчёты. Задай свой chat_id, когда хочешь следить.
    ADMIN_TELEGRAM_CHAT: str = os.getenv("ADMIN_TELEGRAM_CHAT", "").strip()

    # ── Аудио ────────────────────────────────────────────────
    SAMPLE_RATE:     int   = 16000
    CHANNELS:        int   = 1
    FRAME_DURATION:  int   = 30
    SILENCE_SECONDS: float = 2.5
    MAX_SEGMENT_MIN: int   = 2

    # ── Whisper ──────────────────────────────────────────────
    WHISPER_MODEL:    str = "whisper-1"
    WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "ru")

    # ── RMS фильтр тишины (защита от трат на пустой эфир) ─────
    # Аудио тише этого порога (16-bit PCM, 0-32768) считается мёртвым
    # эфиром (микрофон включён, у кассы никого) и НЕ отправляется в STT —
    # экономит деньги OpenAI. 0 = фильтр отключён.
    #
    # Шкала RMS:
    #   0-80    — мёртвый эфир, статика, гул техники (речи физически нет)
    #   120-500 — тихая речь: телефон в 1.5м, тихий сотрудник, дальний клиент
    #   500+    — нормальная речь у микрофона
    #
    # Дефолт 120 — БЕЗОПАСНЫЙ ПОЛ: режет только мёртвый эфир, ниже него
    # человеческой речи не бывает. Тихие разговоры и записи с телефона
    # проходят полностью. Поднимать выше 120 рискованно — можно потерять
    # тихого сотрудника. На шумной точке лучше повышать порог постепенно,
    # сверяясь с логами «RMS фильтр».
    RMS_SILENCE_THRESHOLD: int = int(os.getenv("RMS_SILENCE_THRESHOLD", "120"))

    # ── ISSAI STT — self-hosted faster-whisper (whisper-turbo-ksc2) ────
    # Лучшая открытая модель для казахского (9.16% WER, KSC2 + code-switching).
    # Запустить воркер: docker-compose -f docker-compose.issai.yml up -d
    # Затем указать URL здесь. Если пусто — пропускается.
    ISSAI_WORKER_URL: str = os.getenv("ISSAI_WORKER_URL", "")
    # API-ключ воркера (совпадает с ISSAI_API_KEY на воркере)
    ISSAI_WORKER_KEY: str = os.getenv("ISSAI_WORKER_KEY", "")
    # Окно ожидания ISSAI (сек). ISSAI и OpenAI стартуют ПАРАЛЛЕЛЬНО; OpenAI
    # ждём всегда (надёжная основа), ISSAI — best-effort в пределах этого окна.
    # Если ISSAI занят/медленный (час пик, очередь) и не уложился — его результат
    # игнорируется, работаем на OpenAI. Это снимает ISSAI с блокирующего пути:
    # латентность ограничена этим окном, а не глубиной очереди воркера.
    # Дефолт 25с (было 95): на свободном воркере ISSAI обычно успевает и его
    # казахский домешивается в merge; под нагрузкой не держим слот по 1.5 мин.
    # Данных это не теряет (обработка в фоне, касса уже получила ok) — просто
    # быстрее отпускаем слот обработки. Поднять при медленном воркере.
    ISSAI_GRACE_SECONDS: float = float(os.getenv("ISSAI_GRACE_SECONDS", "25"))

    # ── Русский STT-гейт — self-hosted (бесплатный фильтр болтовни) ─────
    # ВТОРОЙ инстанс того же issai_worker.py, но с базовой моделью, которая
    # хорошо понимает русский (ISSAI_MODEL=openai/whisper-large-v3-turbo) на
    # другом порту. Роль: НЕ финальный транскрипт, а бесплатный гейт — отсеять
    # русскую болтовню кассиров/телефон/фон ДО платного OpenAI STT. Точные слова
    # для фрода всё равно даёт OpenAI. Если URL пуст — гейт пропускается.
    RUSSIAN_WORKER_URL: str = os.getenv("RUSSIAN_WORKER_URL", "")
    RUSSIAN_WORKER_KEY: str = os.getenv("RUSSIAN_WORKER_KEY", "")

    # ── Сборщик обучающих пар (дистилляция ISSAI ← OpenAI) ─────────────
    # Когда включён: после каждого прохода OpenAI STT сохраняем пару
    # (wav в R2 + тексты в БД) для будущего LoRA fine-tuning ISSAI.
    # OpenAI = учитель (ground truth), ISSAI = ученик (исходник для улучшения).
    # По умолчанию OFF — данные клиентов не копим без осознанного решения.
    # Требует: S3_BUCKET + AWS_ACCESS_KEY_ID (для аудио в R2).
    # Только строки с quality_ok=True идут в обучение (≥4 слов + аудио в R2).
    COLLECT_TRAINING_DATA: bool = os.getenv("COLLECT_TRAINING_DATA", "").strip().lower() in ("1", "true", "yes", "on")

    # ── Yandex SpeechKit STT (точное распознавание казахского) ─
    # Если оба значения заданы — включается гибрид: точные казахские
    # слова от Yandex + тон голоса от аудио-модели OpenAI.
    # Получить: Yandex Cloud → сервисный аккаунт → API-ключ + folder id.
    YANDEX_STT_API_KEY:   str = os.getenv("YANDEX_STT_API_KEY", "")
    YANDEX_STT_FOLDER_ID: str = os.getenv("YANDEX_STT_FOLDER_ID", "")
    # Язык распознавания. По умолчанию kk-KZ — модель обучена на казахском
    # и нормально тянет шала-казахский (казахский + русские слова).
    YANDEX_STT_LANG:      str = os.getenv("YANDEX_STT_LANG", "kk-KZ")

    # ── S3 / R2 / Supabase Storage (архив записей для прослушки) ───
    S3_BUCKET:             str = os.getenv("S3_BUCKET", "")
    S3_REGION:             str = os.getenv("S3_REGION", "auto")        # R2 = auto
    S3_ENDPOINT_URL:       str = os.getenv("S3_ENDPOINT_URL", "")      # пусто = AWS, иначе R2/Supabase/MinIO
    AWS_ACCESS_KEY_ID:     str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    # Публичный базовый URL для прослушивания (R2: https://pub-xxxx.r2.dev).
    # Загрузка идёт на S3_ENDPOINT_URL (с подписью), а ссылка в БД — публичная.
    # Если пусто — ссылка строится из S3_ENDPOINT_URL (работает для AWS/Supabase).
    S3_PUBLIC_URL:         str = os.getenv("S3_PUBLIC_URL", "")

    # ── Kaspi ────────────────────────────────────────────────
    KASPI_NUMBER: str = os.getenv("KASPI_NUMBER", "")
    KASPI_NAME:   str = os.getenv("KASPI_NAME", "")

    # ── Email (уведомления о мошенничестве) ─────────────────
    # Вариант 1 (рекомендуется): Resend HTTP API — работает на Render
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    # Вариант 2: любой SMTP (Gmail, Yandex, Brevo…)
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASS: str = os.getenv("SMTP_PASS", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")

    # ── Telegram бот ─────────────────────────────────────────
    # Имя бота без @ — показывается кнопкой "Получить код в Telegram"
    TELEGRAM_BOT_USERNAME: str = os.getenv("TELEGRAM_BOT_USERNAME", "trustcontrol_kzbot")
    # Секрет для проверки подписи Telegram webhook (setWebhook secret_token)
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

    # ── Публичный URL приложения ─────────────────────────────
    # Используется в Telegram-сообщениях (кнопка «Открыть дашборд»)
    # Если не задан — берётся первый домен из ALLOWED_ORIGINS
    _app_url_raw: str = os.getenv("APP_URL", "")
    if not _app_url_raw:
        _origins = os.getenv("ALLOWED_ORIGINS", "")
        _app_url_raw = _origins.split(",")[0].strip() if _origins else ""
    APP_URL: str = _app_url_raw.rstrip("/")

    def validate(self):
        """Log warnings for missing optional env vars — never crash on startup."""
        if not self.OPENAI_API_KEY:
            print("⚠️  WARNING: OPENAI_API_KEY не задан — транскрипция не будет работать")
        if not self.TELEGRAM_BOT_TOKEN:
            print("⚠️  WARNING: TELEGRAM_BOT_TOKEN не задан — уведомления не будут работать")


settings = Settings()
settings.validate()
