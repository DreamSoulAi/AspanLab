# ════════════════════════════════════════════════════════════
#  TrustControl — Dockerfile
#  Multi-stage build: меньший размер образа
# ════════════════════════════════════════════════════════════

# Stage 1: установка зависимостей
FROM python:3.11-slim AS builder

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем зависимости в отдельный слой (кешируется)
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# Stage 2: финальный образ
FROM python:3.11-slim

WORKDIR /app

# Только рантайм зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Копируем установленные пакеты из builder
COPY --from=builder /root/.local /root/.local

# Копируем код проекта
COPY . .

# Создаём непривилегированного пользователя (security)
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Переменные окружения
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Порт
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Запуск
CMD python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
